//! Values and transitions for an owned wait on one linked pull request.

/// Notifications recorded for an exact native session.
pub mod delivery;
/// GitHub issue, pull request and commit identities.
pub mod forge;
/// Durable local entity identifiers.
pub mod id;
/// The native session responsible for an item.
pub mod owner;
/// Decisions made while observing one accepted candidate.
pub mod wait;
