use super::{
    forge::{Oid, PullRef},
    id::Id,
    owner::Owner,
    wait::{ItemKey, ItemName},
};

/// A committed review decision addressed to one native session.
#[derive(Debug)]
pub struct Notice {
    id: Id<Self>,
    item_key: ItemKey,
    item: ItemName,
    pr: PullRef,
    revision: Oid,
    reason: String,
    owner: Owner,
}

impl Notice {
    /// Reconstruct this outbox entry with the owner recorded when the decision was made.
    pub(crate) fn new(
        id: Id<Self>,
        item_key: ItemKey,
        item: ItemName,
        pr: PullRef,
        revision: Oid,
        reason: String,
        owner: Owner,
    ) -> Self {
        Self {
            id,
            item_key,
            item,
            pr,
            revision,
            reason,
            owner,
        }
    }

    /// Identify the local decision for dispatch and acknowledgement.
    pub fn id(&self) -> Id<Self> {
        self.id
    }
    /// Identify the item independently of its display name.
    pub fn item_key(&self) -> &ItemKey {
        &self.item_key
    }
    /// Borrow the name the owner knows this work item by.
    pub fn item(&self) -> &ItemName {
        &self.item
    }
    /// Identify the candidate pull request.
    pub fn pr(&self) -> &PullRef {
        &self.pr
    }
    /// Identify the specific candidate revision for the review.
    pub fn revision(&self) -> &Oid {
        &self.revision
    }
    /// Borrow the decision's original persisted explanation.
    pub fn reason(&self) -> &str {
        &self.reason
    }
    /// Identify the native conversation entitled to this notice.
    pub fn owner(&self) -> &Owner {
        &self.owner
    }
}

/// Evidence supplied by a native adapter after a successful submission.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Receipt {
    /// Codex has accepted the queue entry.
    Queued,
    /// The Claude background command wrote and flushed the notice.
    Emitted,
}

impl Receipt {
    /// Borrow the stable storage representation of the delivery outcome.
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::Queued => "queued",
            Self::Emitted => "emitted",
        }
    }
}

/// Which submission failures can safely be retried, together with the adapter's cause.
#[derive(Debug, thiserror::Error)]
pub enum Failure<E> {
    /// No input was submitted to the native session.
    #[error("native submission did not start")]
    Unsubmitted(#[source] E),
    /// Input may already have reached the session.
    #[error("native acceptance is uncertain")]
    Uncertain(#[source] E),
}

/// The evidence available from one attempt to wake the addressed session.
pub type Submission<E> = Result<Receipt, Failure<E>>;
