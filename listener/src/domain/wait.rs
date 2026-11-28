use super::forge::{IssueRef, Links, Oid, PullRef, State};
use thiserror::Error;

/// The durable item key, including names retained by the preview schema migration.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct ItemKey(String);

/// The human-facing item name in its worktree.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct ItemName(String);

/// Neither a preview key nor an item name can be blank.
#[derive(Debug, Error)]
#[error("item identity must not be blank")]
pub struct EmptyItemError;

impl ItemKey {
    /// Preserve the original key of an item or migrated worktree record.
    pub(crate) fn parse(text: &str) -> Result<Self, EmptyItemError> {
        if text.trim().is_empty() {
            Err(EmptyItemError)
        } else {
            Ok(Self(text.to_owned()))
        }
    }
    /// Borrow the exact persisted spelling.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl ItemName {
    /// Validate the supplied worktree-scoped name without changing its spelling.
    pub fn parse(text: &str) -> Result<Self, EmptyItemError> {
        if text.trim().is_empty() {
            Err(EmptyItemError)
        } else {
            Ok(Self(text.to_owned()))
        }
    }
    /// Borrow the display name used in notifications.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

/// The change in the issue's linked candidate that requires its owner's attention.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ReviewReason {
    /// The issue no longer identifies just one pull request.
    Ambiguous,
    /// The observed pull request or its head differs from the accepted candidate.
    Changed,
    /// The accepted pull request was closed without merging.
    Closed,
}

impl ReviewReason {
    /// Preserve the explanation used in stored decisions and user-visible notices.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Ambiguous => "The issue no longer has exactly one linked PR.",
            Self::Changed => "The linked PR or its accepted revision changed.",
            Self::Closed => "The linked PR closed without merging.",
        }
    }
}

/// A wait transition to persist with the corresponding local action or outbox receipt.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum Event {
    /// Close the item locally against the confirmed merge commit.
    Closeout(Oid),
    /// Deliver this reason to the responsible native session.
    Review(ReviewReason),
}

/// The lifecycle restored from the wait and its atomically committed decisions.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub(crate) enum Progress {
    /// The candidate has no prior decision in this ownership generation.
    Pending,
    /// The owner already has a review decision for this candidate.
    ReviewRequested,
    /// The recorded merge closed the item locally.
    Closed(Oid),
}

/// The sole linked pull request whose observed head matched the local checkout.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Candidate {
    pr: PullRef,
    revision: Oid,
}

/// The forge and checkout do not identify one candidate that can be bound.
#[derive(Debug, Error)]
pub enum AcceptError {
    /// The tracker issue has zero or multiple linked pull requests.
    #[error("the issue must have exactly one linked pull request")]
    Ambiguous,
    /// The checkout does not contain the observed pull request head at HEAD.
    #[error("the linked PR head differs from the checked-out revision")]
    HeadMismatch,
}

impl Candidate {
    /// Accept a new candidate only when the issue's one PR matches the local head.
    pub fn accept(links: &Links, checked_out: &Oid) -> Result<Self, AcceptError> {
        let Links::Single(pull) = links else {
            return Err(AcceptError::Ambiguous);
        };
        if pull.head() != checked_out {
            return Err(AcceptError::HeadMismatch);
        }
        Ok(Self {
            pr: pull.reference().clone(),
            revision: checked_out.clone(),
        })
    }

    /// Hydrate the candidate that was accepted before the repository stored its binding.
    pub(crate) fn restore(pr: PullRef, revision: Oid) -> Self {
        Self { pr, revision }
    }

    /// Identify the pull request accepted for this work.
    pub fn pr(&self) -> &PullRef {
        &self.pr
    }

    /// Identify the checked-out revision that matched the observed PR head.
    pub fn revision(&self) -> &Oid {
        &self.revision
    }
}

/// A work item's wait for its accepted pull request and revision to merge.
#[derive(Debug)]
pub struct Wait {
    item: ItemKey,
    issue: IssueRef,
    candidate: Candidate,
    generation: u64,
    progress: Progress,
    events: Vec<Event>,
}

/// Persisted generations start at one and advance when ownership changes.
#[derive(Debug, Error)]
#[error("wait generation must be positive")]
pub struct GenerationError;

/// A new observation cannot replace an already accepted closeout.
#[derive(Debug, Error)]
#[error("the wait already closed on a different observation")]
pub struct ObserveError;

impl Wait {
    /// Restore a durable lifecycle without publishing decisions that were already committed.
    pub(crate) fn restore(
        item: ItemKey,
        issue: IssueRef,
        candidate: Candidate,
        generation: u64,
        progress: Progress,
    ) -> Result<Self, GenerationError> {
        if generation == 0 {
            return Err(GenerationError);
        }
        Ok(Self {
            item,
            issue,
            candidate,
            generation,
            progress,
            events: Vec::new(),
        })
    }

    /// Identify the durable item this candidate belongs to.
    pub fn item(&self) -> &ItemKey {
        &self.item
    }
    /// Identify the tracker issue whose closing links are observed.
    pub fn issue(&self) -> &IssueRef {
        &self.issue
    }
    /// Identify the pull request accepted at registration.
    pub fn pr(&self) -> &PullRef {
        self.candidate.pr()
    }
    /// Identify the exact head accepted for this wait.
    pub fn revision(&self) -> &Oid {
        self.candidate.revision()
    }
    /// Return the ownership generation the repository must recheck before saving.
    pub fn generation(&self) -> u64 {
        self.generation
    }

    /// Apply a forge observation, recording only the first review or confirmed merge.
    pub fn observe(&mut self, links: &Links) -> Result<(), ObserveError> {
        let event = match links {
            Links::NotSingle => Some(Event::Review(ReviewReason::Ambiguous)),
            Links::Single(pull)
                if pull.reference() != self.pr() || pull.head() != self.revision() =>
            {
                Some(Event::Review(ReviewReason::Changed))
            }
            Links::Single(pull) => match pull.state() {
                State::Open => None,
                State::Closed => Some(Event::Review(ReviewReason::Closed)),
                State::Merged(oid) => Some(Event::Closeout(oid.clone())),
            },
        };
        if let Progress::Closed(merge) = &self.progress {
            return match event {
                Some(Event::Closeout(oid)) if &oid == merge => Ok(()),
                _ => Err(ObserveError),
            };
        }
        if let Some(event) = event {
            self.progress = match &event {
                Event::Closeout(oid) => Progress::Closed(oid.clone()),
                Event::Review(_) if matches!(self.progress, Progress::ReviewRequested) => {
                    return Ok(());
                }
                Event::Review(_) => Progress::ReviewRequested,
            };
            self.events.push(event);
        }
        Ok(())
    }

    /// Borrow outstanding decisions so storage can commit them atomically.
    pub fn events(&self) -> &[Event] {
        &self.events
    }

    /// Acknowledge decisions after their transaction commits.
    pub(crate) fn drain_events(&mut self) -> impl Iterator<Item = Event> {
        self.events.drain(..)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::forge::Pull;

    fn wait() -> Wait {
        Wait::restore(
            ItemKey::parse("old preview key").unwrap(),
            IssueRef::parse("https://github.com/example/repo/issues/6").unwrap(),
            Candidate::restore(
                PullRef::parse("https://github.com/example/repo/pull/10").unwrap(),
                Oid::parse(&"a".repeat(40)).unwrap(),
            ),
            1,
            Progress::Pending,
        )
        .unwrap()
    }

    #[test]
    fn accepting_a_candidate_requires_one_link_at_the_checked_out_head() {
        let pending = wait();
        let revision = pending.revision();
        let linked = Links::Single(Pull::new(
            pending.pr().clone(),
            revision.clone(),
            State::Open,
        ));
        assert_eq!(
            Candidate::accept(&linked, revision).unwrap(),
            pending.candidate
        );
        assert!(matches!(
            Candidate::accept(&Links::NotSingle, revision),
            Err(AcceptError::Ambiguous)
        ));
        let other = Oid::parse(&"b".repeat(40)).unwrap();
        assert!(matches!(
            Candidate::accept(&linked, &other),
            Err(AcceptError::HeadMismatch)
        ));
    }

    #[test]
    fn repeated_observations_buffer_one_review_and_one_later_closeout() {
        let mut wait = wait();
        let merge = Oid::parse(&"b".repeat(40)).unwrap();
        let linked =
            |state| Links::Single(Pull::new(wait.pr().clone(), wait.revision().clone(), state));
        let opened = linked(State::Open);
        let closed = linked(State::Closed);
        let merged = linked(State::Merged(merge.clone()));
        wait.observe(&opened).unwrap();
        assert_eq!(wait.events(), []);
        wait.observe(&closed).unwrap();
        wait.observe(&closed).unwrap();
        assert_eq!(
            wait.drain_events().collect::<Vec<_>>(),
            [Event::Review(ReviewReason::Closed)]
        );
        wait.observe(&merged).unwrap();
        wait.observe(&merged).unwrap();
        assert_eq!(wait.events(), [Event::Closeout(merge)]);
        assert!(wait.observe(&closed).is_err());
    }

    #[test]
    fn a_different_candidate_or_ambiguous_link_never_closes_the_item() {
        let merge = Oid::parse(&"b".repeat(40)).unwrap();
        let accepted = wait();
        let another = PullRef::parse("https://github.com/example/repo/pull/11").unwrap();
        let observations = [
            (Links::NotSingle, ReviewReason::Ambiguous),
            (
                Links::Single(Pull::new(
                    another,
                    accepted.revision().clone(),
                    State::Merged(merge.clone()),
                )),
                ReviewReason::Changed,
            ),
            (
                Links::Single(Pull::new(
                    accepted.pr().clone(),
                    merge.clone(),
                    State::Merged(merge),
                )),
                ReviewReason::Changed,
            ),
        ];
        for (links, reason) in observations {
            let mut aggregate = wait();
            aggregate.observe(&links).unwrap();
            assert_eq!(aggregate.events(), [Event::Review(reason)]);
        }
    }

    #[test]
    fn restoring_a_committed_review_or_closeout_does_not_republish_it() {
        let pending = wait();
        let merge = Oid::parse(&"b".repeat(40)).unwrap();
        let merged = Links::Single(Pull::new(
            pending.pr().clone(),
            pending.revision().clone(),
            State::Merged(merge.clone()),
        ));
        for (progress, observation) in [
            (Progress::ReviewRequested, Links::NotSingle),
            (Progress::Closed(merge), merged),
        ] {
            let mut restored = Wait::restore(
                pending.item().clone(),
                pending.issue().clone(),
                pending.candidate.clone(),
                pending.generation(),
                progress,
            )
            .unwrap();
            restored.observe(&observation).unwrap();
            assert_eq!(restored.events(), []);
        }
    }
}
