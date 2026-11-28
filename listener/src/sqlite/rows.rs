use super::{ReadError, StorageError};
use crate::domain::{
    delivery::Notice,
    forge::{IssueRef, Oid, PullRef},
    id::Id,
    owner::{Host, Owner},
    wait::{Candidate, ItemKey, ItemName, Progress, Wait},
};
use rusqlite::{Connection, Row, params};
use std::{error::Error, path::PathBuf};
use uuid::Uuid;

fn invalid(field: &'static str, source: impl Error + Send + Sync + 'static) -> ReadError {
    ReadError::Invalid {
        field,
        source: Box::new(source),
    }
}

pub(super) struct WaitRow {
    item: String,
    issue: String,
    pr: String,
    revision: String,
    generation: i64,
    state: String,
    condition: String,
    review_requested: bool,
    merge_oid: Option<String>,
}

impl WaitRow {
    pub(super) fn read(row: &Row<'_>) -> rusqlite::Result<Self> {
        Ok(Self {
            item: row.get("item")?,
            issue: row.get("issue")?,
            pr: row.get("pr")?,
            revision: row.get("revision")?,
            generation: row.get("generation")?,
            state: row.get("state")?,
            condition: row.get("condition")?,
            review_requested: row.get("review_requested")?,
            merge_oid: row.get("merge_oid")?,
        })
    }
}

impl TryFrom<WaitRow> for Wait {
    type Error = ReadError;

    fn try_from(row: WaitRow) -> Result<Self, Self::Error> {
        if row.condition != "merged" {
            return Err(ReadError::Condition(row.condition));
        }
        let progress = match (row.state.as_str(), row.merge_oid) {
            ("decided", Some(merge)) => {
                Progress::Closed(Oid::parse(&merge).map_err(|e| invalid("merge_oid", e))?)
            }
            ("pending", None) if row.review_requested => Progress::ReviewRequested,
            ("pending", None) => Progress::Pending,
            _ => return Err(ReadError::Lifecycle),
        };
        Wait::restore(
            ItemKey::parse(&row.item).map_err(|e| invalid("item", e))?,
            IssueRef::parse(&row.issue).map_err(|e| invalid("issue", e))?,
            Candidate::restore(
                PullRef::parse(&row.pr).map_err(|e| invalid("pr", e))?,
                Oid::parse(&row.revision).map_err(|e| invalid("revision", e))?,
            ),
            u64::try_from(row.generation).map_err(|e| invalid("generation", e))?,
            progress,
        )
        .map_err(|e| invalid("generation", e))
    }
}

pub(super) struct Pending {
    pub(super) wait: Wait,
    pub(super) notice: Option<Notice>,
}

impl Pending {
    fn read(row: &Row<'_>) -> Result<Self, ReadError> {
        let wait = Wait::try_from(WaitRow::read(row).map_err(ReadError::from_column)?)?;
        let host: String = row.get("host").map_err(ReadError::from_column)?;
        let thread: String = row.get("thread").map_err(ReadError::from_column)?;
        let profile: String = row.get("profile").map_err(ReadError::from_column)?;
        let owner = Owner::new(
            Host::parse(&host).map_err(|e| invalid("host", e))?,
            PathBuf::from(profile),
            Uuid::parse_str(&thread).map_err(|e| invalid("thread", e))?,
        )
        .map_err(|e| invalid("profile", e))?;
        let notice = if let Some(id) = row
            .get::<_, Option<i64>>("notice_id")
            .map_err(ReadError::from_column)?
        {
            let name: String = row.get("display_name").map_err(ReadError::from_column)?;
            Some(Notice::new(
                Id::new(u64::try_from(id).map_err(|e| invalid("notice id", e))?)
                    .map_err(|e| invalid("notice id", e))?,
                wait.item().clone(),
                ItemName::parse(&name).map_err(|e| invalid("notice item name", e))?,
                wait.pr().clone(),
                wait.revision().clone(),
                row.get("reason").map_err(ReadError::from_column)?,
                owner,
            ))
        } else {
            None
        };
        Ok(Self { wait, notice })
    }
}

impl ReadError {
    fn from_column(error: rusqlite::Error) -> Self {
        match error {
            rusqlite::Error::InvalidColumnType(..)
            | rusqlite::Error::FromSqlConversionFailure(..)
            | rusqlite::Error::IntegralValueOutOfRange(..)
            | rusqlite::Error::Utf8Error(..) => invalid("workflow column", error),
            other => Self::Storage(StorageError::from(other)),
        }
    }
}

pub(super) struct Scan {
    pub(super) pending: Vec<Pending>,
    pub(super) errors: Vec<(i64, String)>,
}

impl Scan {
    pub(super) fn read(
        db: &Connection,
        host: Option<Host>,
        owner: Option<&Owner>,
    ) -> Result<Self, ReadError> {
        let mut stmt = db
            .prepare(
                "SELECT s.*, w.rowid AS wait_id, w.last_error, w.host, w.profile, w.thread,
                    n.id AS notice_id, n.item AS display_name, n.reason
             FROM pr_wait_snapshot s JOIN pr_waits w ON w.item IS s.item
             LEFT JOIN pr_deliverable n ON n.item_key=w.item AND n.delivery='pending'
             WHERE s.state='pending' AND (?1 IS NULL OR n.host=?1)
               AND (?2 IS NULL OR n.profile=?2) AND (?3 IS NULL OR n.thread=?3)
             ORDER BY CASE WHEN ?1 IS NOT NULL THEN n.id END, s.item",
            )
            .map_err(StorageError::from)?;
        let mut rows = stmt
            .query(params![
                host.map(Host::as_str),
                owner.map(Owner::profile_str),
                owner.map(|o| o.thread().to_string()),
            ])
            .map_err(StorageError::from)?;
        let mut pending = Vec::new();
        let mut errors = Vec::new();
        while let Some(row) = rows.next().map_err(StorageError::from)? {
            match Pending::read(row) {
                Ok(record) => pending.push(record),
                Err(error @ ReadError::Storage(_)) => return Err(error),
                Err(error) => {
                    let diagnostic = error.to_string();
                    if row.get_ref("last_error").map_err(StorageError::from)?
                        != rusqlite::types::ValueRef::Text(diagnostic.as_bytes())
                    {
                        errors.push((row.get("wait_id").map_err(StorageError::from)?, diagnostic));
                    }
                }
            }
        }
        Ok(Self { pending, errors })
    }
}
