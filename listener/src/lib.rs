//! The linked-PR wait, its GitHub observations and its durable delivery receipts.

/// Values and transitions shared by the native adapters.
pub mod domain;
/// GitHub's closing pull-request links.
pub mod github;
/// The SQLite repository shared with managed terminal sessions.
pub mod sqlite;
/// Registered Git worktrees and their filesystem identities.
pub mod worktree;

/// The native session identifier used by both supported hosts.
pub use uuid::Uuid;
