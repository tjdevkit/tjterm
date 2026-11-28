use crate::github::{Links, State};
use anyhow::{Context, Result, ensure};
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
use serde::Serialize;
use std::{fs, path::Path, time::Duration};

const LEGACY: &str = include_str!("../../src/tjentic/workflows/schema.sql");
const SCHEMA: &str = include_str!("schema.sql");
const ELIGIBLE: &str = "SELECT EXISTS(SELECT 1 FROM pr_waits w JOIN items i ON i.id=w.item
WHERE w.item=?1 AND w.pr=?2 AND w.revision=?3 AND w.generation=?4 AND w.state='pending'
AND i.thread=w.thread AND i.hold IS NULL AND i.phase IN ('assigned','ready')
AND (i.phase!='ready' OR EXISTS(SELECT 1 FROM checks c WHERE c.item=i.id AND c.revision=w.revision AND c.exit_code=0 AND c.attempt=(SELECT MAX(attempt) FROM checks WHERE item=i.id)))
AND NOT EXISTS(SELECT 1 FROM transfers t LEFT JOIN deliveries d ON d.id=t.action_delivery WHERE t.item=i.id AND (t.state='offered' OR (t.state='accepted' AND d.state!='done'))))";

pub struct Store(Connection);

#[derive(Debug)]
pub struct Wait {
    pub item: String,
    pub issue: String,
    pub pr: String,
    pub revision: String,
    pub generation: i64,
}

pub struct Binding<'a> {
    pub item: &'a str,
    pub issue: &'a str,
    pub pr: &'a str,
    pub revision: &'a str,
    pub host: &'a str,
    pub profile: &'a str,
    pub thread: &'a str,
    pub root: &'a str,
    pub git_dir: &'a str,
    pub device: &'a str,
    pub inode: &'a str,
}

#[derive(Serialize)]
pub struct Notice {
    pub id: i64,
    pub item: String,
    pub pr: String,
    pub revision: String,
    pub reason: String,
    pub profile: String,
    pub thread: String,
}

impl Store {
    pub fn open(state: &Path) -> Result<Self> {
        ensure!(
            state.is_absolute(),
            "workflow state directory must be absolute"
        );
        fs::create_dir_all(state)?;
        let mut db = Connection::open(state.join("workflows.sqlite3"))?;
        db.busy_timeout(Duration::from_secs(10))?;
        db.pragma_update(None, "foreign_keys", true)?;
        // Keep the preview's rollback journal. IMMEDIATE serializes the first migration and decisions.
        let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        tx.execute_batch(LEGACY)?;
        let has_id: bool = tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM pragma_table_info('items') WHERE name='id')",
            [],
            |r| r.get(0),
        )?;
        if !has_id {
            tx.execute_batch(include_str!("../../src/tjentic/workflows/item_ids.sql"))?;
        }
        tx.execute_batch(
            "CREATE UNIQUE INDEX IF NOT EXISTS item_name_in_worktree ON items(worktree,name)",
        )?;
        let version: i64 = tx.pragma_query_value(None, "user_version", |row| row.get(0))?;
        ensure!(
            version <= 1,
            "unsupported workflow schema version {version}"
        );
        if version == 0 {
            tx.execute_batch(SCHEMA)?;
            tx.pragma_update(None, "user_version", 1)?;
        }
        tx.commit()?;
        Ok(Self(db))
    }

    pub fn bind(&mut self, b: &Binding<'_>) -> Result<Wait> {
        let tx = self
            .0
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        tx.execute(
            "INSERT INTO items(id,name,thread,directory,worktree,git_dir,command,phase,host)
            VALUES(lower(hex(randomblob(16))),?1,?2,?3,?3,?4,'[]','assigned',?5)
            ON CONFLICT(worktree,name) DO NOTHING",
            params![b.item, b.thread, b.root, b.git_dir, b.host],
        )?;
        let (identity, git_dir): (String, String) = tx
            .query_row(
                "SELECT id,git_dir FROM items WHERE name=? AND thread=? AND worktree=? AND host=?",
                params![b.item, b.thread, b.root, b.host],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?
            .context("this item belongs to a different owner or worktree")?;
        ensure!(
            Path::new(&git_dir) == Path::new(b.git_dir),
            "the registered Git directory for this item changed"
        );
        tx.execute(
            "INSERT OR IGNORE INTO worktree_ids(item,device,inode) VALUES(?,?,?)",
            params![identity, b.device, b.inode],
        )?;
        let matches: bool = tx.query_row(
            "SELECT device=? AND inode=? FROM worktree_ids WHERE item=?",
            params![b.device, b.inode, identity],
            |r| r.get(0),
        )?;
        ensure!(matches, "the registered worktree has been replaced");
        let count=tx.execute("INSERT INTO pr_waits(item,issue,pr,revision,host,profile,thread) VALUES(?1,?2,?3,?4,?5,?6,?7)
            ON CONFLICT(item) DO UPDATE SET host=excluded.host,profile=excluded.profile,thread=excluded.thread,
            revision=excluded.revision,state=CASE WHEN pr_waits.state='decided' THEN 'decided' ELSE 'pending' END,last_error=NULL
            WHERE pr_waits.issue=excluded.issue AND pr_waits.pr=excluded.pr
            AND (pr_waits.state='rebind' OR (pr_waits.host=excluded.host AND pr_waits.profile=excluded.profile AND pr_waits.thread=excluded.thread))
            AND (pr_waits.state!='decided' OR pr_waits.revision=excluded.revision)",params![identity,b.issue,b.pr,b.revision,b.host,b.profile,b.thread])?;
        ensure!(
            count == 1,
            "item already has a different linked PR, owner or completed decision"
        );
        let generation = tx.query_row(
            "SELECT generation FROM pr_waits WHERE item=?",
            [&identity],
            |r| r.get(0),
        )?;
        tx.commit()?;
        Ok(Wait {
            item: identity,
            issue: b.issue.into(),
            pr: b.pr.into(),
            revision: b.revision.into(),
            generation,
        })
    }

    pub fn has_closeout(&self, wait: &Wait) -> Result<bool> {
        Ok(self.0.query_row("SELECT EXISTS(SELECT 1 FROM pr_decisions WHERE item=? AND pr=? AND revision=? AND kind='closeout')",params![wait.item,wait.pr,wait.revision],|row|row.get(0))?)
    }

    pub fn waits(&self) -> Result<Vec<Wait>> {
        let mut stmt=self.0.prepare("SELECT item,issue,pr,revision,generation FROM pr_waits WHERE state='pending' ORDER BY item")?;
        Ok(stmt
            .query_map([], |r| {
                Ok(Wait {
                    item: r.get(0)?,
                    issue: r.get(1)?,
                    pr: r.get(2)?,
                    revision: r.get(3)?,
                    generation: r.get(4)?,
                })
            })?
            .collect::<Result<_, _>>()?)
    }

    pub fn error(&self, w: &Wait, error: &str) -> Result<()> {
        self.0.execute("UPDATE pr_waits SET last_error=? WHERE item=? AND generation=? AND revision=? AND state='pending'",params![error,w.item,w.generation,w.revision])?;
        Ok(())
    }

    pub fn observe(&mut self, w: &Wait, links: &Links) -> Result<()> {
        let tx = self
            .0
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        if !tx.query_row(
            ELIGIBLE,
            params![w.item, w.pr, w.revision, w.generation],
            |r| r.get::<_, bool>(0),
        )? {
            return Ok(());
        }
        tx.execute(
            "UPDATE pr_waits SET last_error=NULL WHERE item=?",
            [&w.item],
        )?;
        let (kind, reason, merge) = match links.single() {
            None => (
                "review",
                "The issue no longer has exactly one linked PR.",
                None,
            ),
            Some(p) if p.url != w.pr || p.head_ref_oid != w.revision => (
                "review",
                "The linked PR or its accepted revision changed.",
                None,
            ),
            Some(p) => match p.state {
                State::Open => {
                    tx.commit()?;
                    return Ok(());
                }
                State::Closed => ("review", "The linked PR closed without merging.", None),
                State::Merged => (
                    "closeout",
                    "The accepted revision merged.",
                    Some(
                        p.merge_commit
                            .as_ref()
                            .ok_or_else(|| anyhow::anyhow!("merged PR has no merge commit"))?
                            .oid
                            .as_str(),
                    ),
                ),
            },
        };
        tx.execute("INSERT INTO pr_decisions(item,pr,revision,merge_oid,host,profile,thread,generation,kind,reason,delivery)
         SELECT item,pr,revision,?2,host,profile,thread,generation,?3,?4,?5 FROM pr_waits WHERE item=?1 ON CONFLICT DO NOTHING",
         params![w.item,merge,kind,reason,if merge.is_some() {"recorded"}else{"pending"}])?;
        if merge.is_some() {
            tx.execute(
                "UPDATE pr_waits SET state='decided' WHERE item=?",
                [&w.item],
            )?;
            tx.execute("UPDATE items SET phase='closed' WHERE id=?", [&w.item])?;
        }
        tx.commit()?;
        Ok(())
    }

    pub fn claim_notice(
        &mut self,
        host: &str,
        profile: Option<&str>,
        thread: Option<&str>,
    ) -> Result<Option<Notice>> {
        let tx = self
            .0
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let event = tx
            .query_row(
                "SELECT id,item,pr,revision,reason,profile,thread FROM pr_deliverable
             WHERE host=?1 AND delivery='pending' AND (?2 IS NULL OR profile=?2)
             AND (?3 IS NULL OR thread=?3) ORDER BY id LIMIT 1",
                params![host, profile, thread],
                |r| {
                    Ok(Notice {
                        id: r.get(0)?,
                        item: r.get(1)?,
                        pr: r.get(2)?,
                        revision: r.get(3)?,
                        reason: r.get(4)?,
                        profile: r.get(5)?,
                        thread: r.get(6)?,
                    })
                },
            )
            .optional()?;
        if let Some(n) = &event {
            tx.execute(
                "UPDATE pr_decisions SET delivery='dispatching' WHERE id=?",
                [n.id],
            )?;
        }
        tx.commit()?;
        Ok(event)
    }

    /// Release a claim if the adapter failed before submitting anything.
    pub fn retry_unsubmitted(&self, id: i64) -> Result<()> {
        self.0.execute(
            "UPDATE pr_decisions SET delivery='pending' WHERE id=? AND delivery='dispatching'",
            [id],
        )?;
        Ok(())
    }

    /// Serialize the last ownership check and native send with handoffs. A crash
    /// after native acceptance leaves the prior dispatching claim for inspection.
    pub fn dispatch(
        &mut self,
        notice: &Notice,
        send: impl FnOnce(&Notice) -> Result<&'static str>,
    ) -> Result<()> {
        let tx = self
            .0
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let eligible: bool = tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM pr_deliverable WHERE id=? AND delivery='dispatching')",
            [notice.id],
            |r| r.get(0),
        )?;
        let outcome = if eligible { send(notice)? } else { "pending" };
        tx.execute(
            "UPDATE pr_decisions SET delivery=? WHERE id=? AND delivery='dispatching'",
            params![outcome, notice.id],
        )?;
        tx.commit()?;
        Ok(())
    }
}
