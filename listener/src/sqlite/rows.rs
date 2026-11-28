use super::ReadError;
use crate::domain::{
    delivery::Notice,
    forge::{IssueRef, Oid, PullRef},
    id::Id,
    owner::{Host, Owner},
    wait::{Candidate, ItemKey, ItemName, Progress, Wait},
};
use rusqlite::Row;
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
            review_requested: row.get("review_requested")?,
            merge_oid: row.get("merge_oid")?,
        })
    }
}

impl TryFrom<WaitRow> for Wait {
    type Error = ReadError;

    fn try_from(row: WaitRow) -> Result<Self, Self::Error> {
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

pub(super) struct NoticeRow {
    id: i64,
    key: String,
    name: String,
    pr: String,
    revision: String,
    reason: String,
    host: String,
    profile: String,
    thread: String,
}

impl NoticeRow {
    pub(super) fn read(row: &Row<'_>) -> rusqlite::Result<Self> {
        Ok(Self {
            id: row.get("id")?,
            key: row.get("item_key")?,
            name: row.get("item")?,
            pr: row.get("pr")?,
            revision: row.get("revision")?,
            reason: row.get("reason")?,
            host: row.get("host")?,
            profile: row.get("profile")?,
            thread: row.get("thread")?,
        })
    }
}

impl TryFrom<NoticeRow> for Notice {
    type Error = ReadError;

    fn try_from(row: NoticeRow) -> Result<Self, Self::Error> {
        let owner = Owner::new(
            Host::parse(&row.host).map_err(|e| invalid("host", e))?,
            PathBuf::from(row.profile),
            Uuid::parse_str(&row.thread).map_err(|e| invalid("thread", e))?,
        )
        .map_err(|e| invalid("profile", e))?;
        Ok(Notice::new(
            Id::new(u64::try_from(row.id).map_err(|e| invalid("id", e))?)
                .map_err(|e| invalid("id", e))?,
            ItemKey::parse(&row.key).map_err(|e| invalid("item_key", e))?,
            ItemName::parse(&row.name).map_err(|e| invalid("name", e))?,
            PullRef::parse(&row.pr).map_err(|e| invalid("pr", e))?,
            Oid::parse(&row.revision).map_err(|e| invalid("revision", e))?,
            row.reason,
            owner,
        ))
    }
}
