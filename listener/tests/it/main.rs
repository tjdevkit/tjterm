//! Listener integration tests against native subprocesses and real `SQLite` databases.

#[cfg(test)]
mod dispatch;
#[cfg(test)]
mod migrations;
#[cfg(test)]
mod waits;

use rusqlite::Connection;
use std::path::Path;

const THREAD: &str = "019a0000-0000-7000-8000-000000000001";
const NEXT: &str = "019a0000-0000-7000-8000-000000000002";
const ISSUE: &str = "https://github.com/paper/widget/issues/7";
const PR: &str = "https://github.com/paper/widget/pull/17";
const SHA: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const MERGE: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

fn sql(path: &Path) -> Connection {
    Connection::open(path.join("workflows.sqlite3")).expect("open the fixture database")
}
