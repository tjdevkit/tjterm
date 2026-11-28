//! `SQLite` persistence for linked PR waits and their delivery receipts.

mod rows;

use crate::{
    domain::{
        delivery::{Failure, Notice, Submission},
        forge::IssueRef,
        id::Id,
        owner::{Host, Owner},
        wait::{Candidate, Event, ItemName, ReplaceError, Wait},
    },
    worktree::Worktree,
};
use rows::{Pending, Scan, WaitRow};
use rusqlite::{
    Connection, ErrorCode, OptionalExtension, Transaction, TransactionBehavior, params,
};
use std::{error::Error, fs, path::Path, time::Duration};
use thiserror::Error;

const BASE: &str = include_str!("../../src/tjentic/workflows/schema.sql");
const ITEM_IDS: &str = include_str!("../../src/tjentic/workflows/item_ids.sql");
const FIRST: &str = include_str!("schema.sql");
const DELIVERY: &str = include_str!("sqlite/002-delivery.sql");

/// A storage operation may fail on a conflicting record, an unsupported number or a database error.
#[derive(Debug, Error)]
pub enum StorageError {
    /// `SQLite`'s signed integer range cannot represent the supplied identifier.
    #[error("workflow number is outside the SQLite integer range")]
    Integer(#[from] std::num::TryFromIntError),
    /// Existing data conflicts with a storage invariant.
    #[error("workflow database constraint violation")]
    Constraint(#[source] Box<dyn Error + Send + Sync>),
    /// The requested SQL operation did not complete.
    #[error("workflow database operation failed")]
    Database(#[source] Box<dyn Error + Send + Sync>),
}

impl From<rusqlite::Error> for StorageError {
    fn from(error: rusqlite::Error) -> Self {
        if error.sqlite_error_code() == Some(ErrorCode::ConstraintViolation) {
            Self::Constraint(Box::new(error))
        } else {
            Self::Database(Box::new(error))
        }
    }
}

/// The workflow database could not be opened and migrated.
#[derive(Debug, Error)]
pub enum OpenError {
    /// The state directory must have a stable location.
    #[error("workflow state directory must be absolute")]
    RelativePath,
    /// The state directory could not be created.
    #[error("cannot create the workflow state directory")]
    Directory(#[source] std::io::Error),
    /// Opening or migrating the database failed.
    #[error(transparent)]
    Storage(#[from] StorageError),
    /// The database requires another schema version.
    #[error("unsupported workflow schema version {0}")]
    Version(i64),
}

/// Stored workflow records must satisfy the same invariants as newly registered data.
#[derive(Debug, Error)]
pub enum ReadError {
    /// The database could not supply the records.
    #[error(transparent)]
    Storage(#[from] StorageError),
    /// A record has an invalid value for a domain field.
    #[error("invalid stored {field}: {source}")]
    Invalid {
        /// The field containing the invalid value.
        field: &'static str,
        /// Its validation failure.
        #[source]
        source: Box<dyn Error + Send + Sync>,
    },
    /// The listener has no observation for this stored pending condition.
    #[error("unsupported wait condition: {0}")]
    Condition(String),
    /// The stored wait and its durable closeout evidence disagree.
    #[error("stored wait has an inconsistent closeout receipt")]
    Lifecycle,
}

/// Registration failed before binding the accepted revision.
#[derive(Debug, Error)]
pub enum BindError {
    /// The existing wait cannot accept the owner's new candidate.
    #[error(transparent)]
    Replacement(#[from] ReplaceError),

    /// One of the records could not be written.
    #[error(transparent)]
    Storage(#[from] StorageError),
    /// A stored identity or generation cannot be used.
    #[error(transparent)]
    Read(#[from] ReadError),
    /// This name already belongs to another owner in the checkout.
    #[error("this item belongs to a different owner or worktree")]
    Owner,
    /// The directory identity no longer matches the binding.
    #[error("the registered Git worktree has been replaced")]
    Worktree,
    /// Paths used by both Python and Rust must be representable as `SQLite` text.
    #[error("registered workflow paths must be valid UTF-8")]
    Path,
    /// The existing issue, owner, condition or closed item prevents this registration.
    #[error("registration conflicts with the existing issue, owner, wait condition or closed item")]
    Candidate,
}

/// Finishing a native submission requires the same claim that authorised it.
#[derive(Debug, Error)]
pub enum FinishError {
    /// No receipt was recorded.
    #[error(transparent)]
    Storage(#[from] StorageError),
    /// The decision no longer has the supplied active claim.
    #[error("the delivery claim no longer owns the decision")]
    LostClaim,
}

/// Registration inputs verified by the command, native host and Git.
pub struct Binding<'a> {
    /// Worktree-scoped display name.
    pub item: &'a ItemName,
    /// Tracker issue authorising the wait.
    pub issue: &'a IssueRef,
    /// The candidate accepted against the local checkout.
    pub candidate: &'a Candidate,
    /// The exact native conversation and profile.
    pub owner: &'a Owner,
    /// The Git directory and filesystem identity verified at registration.
    pub worktree: &'a Worktree,
}

/// Exclusive right to finish a single submission attempt.
#[derive(Debug)]
pub struct Claim {
    id: Id<Notice>,
    token: String,
}

/// Whether the current binding still authorised this observation at commit time.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum SaveOutcome {
    /// The observed transitions are durable.
    Saved,
    /// Eligibility changed; none of the aggregate's outstanding transitions were saved.
    Ineligible,
}

/// Owns the connection and its short workflow and outbox transactions.
pub struct Store(Connection);

impl Store {
    /// Open the local workflow database, atomically upgrading all older readers' data.
    pub fn open(state: &Path) -> Result<Self, OpenError> {
        if !state.is_absolute() {
            return Err(OpenError::RelativePath);
        }
        fs::create_dir_all(state).map_err(OpenError::Directory)?;
        let mut db =
            Connection::open(state.join("workflows.sqlite3")).map_err(StorageError::from)?;
        db.busy_timeout(Duration::from_secs(10))
            .map_err(StorageError::from)?;
        db.pragma_update(None, "foreign_keys", true)
            .map_err(StorageError::from)?;
        // Retain the preview's rollback journal; do not change journal mode under another client.
        let tx = db
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(StorageError::from)?;
        let version: i64 = tx
            .pragma_query_value(None, "user_version", |r| r.get(0))
            .map_err(StorageError::from)?;
        match version {
            0 => {
                tx.execute_batch(BASE).map_err(StorageError::from)?;
                let has_id: bool = tx
                    .query_row(
                        "SELECT EXISTS(SELECT 1 FROM pragma_table_info('items') WHERE name='id')",
                        [],
                        |r| r.get(0),
                    )
                    .map_err(StorageError::from)?;
                if !has_id {
                    tx.execute_batch(ITEM_IDS).map_err(StorageError::from)?;
                }
                tx.execute_batch(
                    "CREATE UNIQUE INDEX IF NOT EXISTS item_name_in_worktree ON items(worktree,name)"
                ).map_err(StorageError::from)?;
                tx.execute_batch(FIRST).map_err(StorageError::from)?;
            }
            1 | 2 => {}
            other => return Err(OpenError::Version(other)),
        }
        if version < 2 {
            tx.execute_batch(DELIVERY).map_err(StorageError::from)?;
            tx.pragma_update(None, "user_version", 2)
                .map_err(StorageError::from)?;
        }
        tx.commit().map_err(StorageError::from)?;
        Ok(Self(db))
    }

    /// Register this exact item, owner, checkout, and accepted Git revision.
    pub fn bind(&mut self, b: &Binding<'_>) -> Result<Wait, BindError> {
        let root = b.worktree.root().to_str().ok_or(BindError::Path)?;
        let git_dir = b.worktree.git_dir().to_str().ok_or(BindError::Path)?;
        let profile = b.owner.profile_str();
        let host = b.owner.host().as_str();
        let thread = b.owner.thread().to_string();
        let tx = self
            .0
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(StorageError::from)?;
        tx.execute(
            "INSERT INTO items(id,name,thread,directory,worktree,git_dir,command,phase,host)
             VALUES(lower(hex(randomblob(16))),?1,?2,?3,?3,?4,'[]','assigned',?5)
             ON CONFLICT(worktree,name) DO NOTHING",
            params![b.item.as_str(), thread, root, git_dir, host],
        )
        .map_err(StorageError::from)?;
        let (key, bound_git_dir): (String, String) = tx
            .query_row(
                "SELECT id,git_dir FROM items WHERE name=? AND thread=? AND worktree=? AND host=?",
                params![b.item.as_str(), thread, root, host],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()
            .map_err(StorageError::from)?
            .ok_or(BindError::Owner)?;
        if Path::new(&bound_git_dir) != b.worktree.git_dir() {
            return Err(BindError::Worktree);
        }
        let device = b.worktree.device().to_string();
        let inode = b.worktree.inode().to_string();
        tx.execute(
            "INSERT INTO worktree_ids(item,device,inode) VALUES(?,?,?) ON CONFLICT(item) DO NOTHING",
            params![key,device,inode],
        ).map_err(StorageError::from)?;
        let matches: bool = tx
            .query_row(
                "SELECT device=? AND inode=? FROM worktree_ids WHERE item=?",
                params![device, inode, key],
                |r| r.get(0),
            )
            .map_err(StorageError::from)?;
        if !matches {
            return Err(BindError::Worktree);
        }
        let changed = tx
            .execute(
                include_str!("sqlite/bind.sql"),
                params![
                    key,
                    b.issue.as_str(),
                    b.candidate.pr().as_str(),
                    b.candidate.revision().as_str(),
                    host,
                    profile,
                    thread
                ],
            )
            .map_err(StorageError::from)?;
        if changed != 1 {
            return Err(BindError::Candidate);
        }
        let row = tx
            .query_row(
                "SELECT * FROM pr_wait_snapshot WHERE item=?",
                [&key],
                WaitRow::read,
            )
            .map_err(StorageError::from)?;
        let mut wait = Wait::try_from(row)?;
        let previous = wait.generation();
        wait.replace_candidate(b.candidate)?;
        if wait.generation() != previous {
            let updated = tx
                .execute(
                    "UPDATE pr_waits SET pr=?2, revision=?3, generation=?4
                 WHERE item=?1 AND generation=?5
                 AND EXISTS (SELECT 1 FROM items WHERE id=?1 AND phase!='closed')",
                    params![
                        key,
                        wait.pr().as_str(),
                        wait.revision().as_str(),
                        i64::try_from(wait.generation()).map_err(StorageError::from)?,
                        i64::try_from(previous).map_err(StorageError::from)?,
                    ],
                )
                .map_err(StorageError::from)?;
            if updated != 1 {
                return Err(BindError::Candidate);
            }
        }
        tx.commit().map_err(StorageError::from)?;
        Ok(wait)
    }

    /// Check the durable closeout for precisely this PR and revision.
    pub fn has_closeout(&self, wait: &Wait) -> Result<bool, StorageError> {
        self.0
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM pr_decisions
             WHERE item=? AND pr=? AND revision=? AND kind='closeout')",
                params![
                    wait.item().as_str(),
                    wait.pr().as_str(),
                    wait.revision().as_str()
                ],
                |r| r.get(0),
            )
            .map_err(StorageError::from)
    }

    /// Read pending waits, including held work that may become eligible again.
    pub fn waits(&self) -> Result<Vec<Wait>, ReadError> {
        Ok(self
            .pending(None, None)?
            .into_iter()
            .map(|r| r.wait)
            .collect())
    }

    fn pending(
        &self,
        host: Option<Host>,
        owner: Option<&Owner>,
    ) -> Result<Vec<Pending>, ReadError> {
        let first = Scan::read(&self.0, host, owner)?;
        if first.errors.is_empty() {
            return Ok(first.pending);
        }
        // Recheck after acquiring the writer; another process may have repaired or rebound the item.
        let tx = Transaction::new_unchecked(&self.0, TransactionBehavior::Immediate)
            .map_err(StorageError::from)?;
        let current = Scan::read(&tx, host, owner)?;
        for (id, error) in current.errors {
            tx.execute(
                "UPDATE pr_waits SET last_error=? WHERE rowid=?",
                params![error, id],
            )
            .map_err(StorageError::from)?;
        }
        tx.commit().map_err(StorageError::from)?;
        Ok(current.pending)
    }

    /// Record a transient observation error only if the binding is still current.
    pub fn record_error(&self, wait: &Wait, error: &str) -> Result<(), StorageError> {
        self.0.execute(
            "UPDATE pr_waits SET last_error=? WHERE item=? AND issue=? AND pr=?
             AND revision=? AND generation=? AND state='pending' AND condition='merged'",
            params![
                error,
                wait.item().as_str(),
                wait.issue().as_str(),
                wait.pr().as_str(),
                wait.revision().as_str(),
                i64::try_from(wait.generation())?
            ],
        )?;
        Ok(())
    }

    /// Commit the observed transitions and their outbox records together.
    pub fn save(&mut self, wait: &mut Wait) -> Result<SaveOutcome, StorageError> {
        let tx = self
            .0
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let item = wait.item().as_str();
        let eligible: bool = tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM pr_eligible WHERE item=? AND issue=? AND pr=?
             AND revision=? AND generation=?)",
            params![
                item,
                wait.issue().as_str(),
                wait.pr().as_str(),
                wait.revision().as_str(),
                i64::try_from(wait.generation())?
            ],
            |r| r.get(0),
        )?;
        if !eligible {
            tx.commit()?;
            return Ok(SaveOutcome::Ineligible);
        }
        tx.execute("UPDATE pr_waits SET last_error=NULL WHERE item=?", [item])?;
        for event in wait.events() {
            let (kind, reason, merge, delivery) = match event {
                Event::Review(reason) => ("review", reason.as_str(), None, "pending"),
                Event::Closeout(oid) => (
                    "closeout",
                    "The accepted revision merged.",
                    Some(oid.as_str()),
                    "recorded",
                ),
            };
            tx.execute(
                    "INSERT INTO pr_decisions(item,pr,revision,merge_oid,host,profile,thread,generation,kind,reason,delivery)
                     SELECT item,pr,revision,?2,host,profile,thread,generation,?3,?4,?5
                     FROM pr_waits WHERE item=?1 ON CONFLICT DO NOTHING",
                    params![item,merge,kind,reason,delivery],
                )?;
            if merge.is_some() {
                tx.execute("UPDATE pr_waits SET state='decided' WHERE item=?", [item])?;
                tx.execute("UPDATE items SET phase='closed' WHERE id=?", [item])?;
            }
        }
        tx.commit()?;
        drop(wait.drain_events());
        Ok(SaveOutcome::Saved)
    }

    /// Snapshot eligible pending notices so one rejected preflight cannot starve other items.
    pub fn notices(&self, host: Host, owner: Option<&Owner>) -> Result<Vec<Notice>, ReadError> {
        if owner.is_some_and(|o| o.host() != host) {
            return Ok(Vec::new());
        }
        Ok(self
            .pending(Some(host), owner)?
            .into_iter()
            .filter_map(|r| r.notice)
            .collect())
    }

    /// Commit exclusive authorisation immediately before submitting outside `SQLite`.
    pub fn begin_submission(&mut self, notice: &Notice) -> Result<Option<Claim>, StorageError> {
        let tx = self
            .0
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let token: Option<String> = tx
            .query_row(
                "UPDATE pr_decisions
                 SET delivery='dispatching', dispatch_token=lower(hex(randomblob(16)))
                 WHERE id=?1 AND item=?2 AND pr=?3 AND revision=?4
                   AND host=?5 AND profile=?6 AND thread=?7
                   AND id IN (SELECT id FROM pr_deliverable WHERE id=?1 AND delivery='pending')
                 RETURNING dispatch_token",
                params![
                    i64::try_from(notice.id().get())?,
                    notice.item_key().as_str(),
                    notice.pr().as_str(),
                    notice.revision().as_str(),
                    notice.owner().host().as_str(),
                    notice.owner().profile_str(),
                    notice.owner().thread().to_string(),
                ],
                |r| r.get(0),
            )
            .optional()?;
        tx.commit()?;
        Ok(token.map(|token| Claim {
            id: notice.id(),
            token,
        }))
    }

    /// Record only what the native adapter knows, fenced to its exclusive claim.
    pub fn finish<E>(&self, claim: Claim, outcome: &Submission<E>) -> Result<(), FinishError> {
        let delivery = match outcome {
            Ok(receipt) => receipt.as_str(),
            Err(Failure::Unsubmitted(_)) => "pending",
            Err(Failure::Uncertain(_)) => "uncertain",
        };
        let changed = self
            .0
            .execute(
                "UPDATE pr_decisions SET delivery=?,dispatch_token=NULL
             WHERE id=? AND dispatch_token=? AND delivery='dispatching'",
                (
                    delivery,
                    i64::try_from(claim.id.get()).map_err(StorageError::from)?,
                    claim.token,
                ),
            )
            .map_err(StorageError::from)?;
        if changed != 1 {
            return Err(FinishError::LostClaim);
        }
        Ok(())
    }
}
