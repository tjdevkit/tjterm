import hashlib
import json
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from tjentic.errors import TjenticError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
    id TEXT PRIMARY KEY, name TEXT NOT NULL, thread TEXT NOT NULL,
    directory TEXT NOT NULL,
    worktree TEXT NOT NULL, git_dir TEXT NOT NULL, command TEXT NOT NULL,
    phase TEXT NOT NULL, hold TEXT, last_error TEXT,
    host TEXT NOT NULL DEFAULT 'codex'
);
CREATE TABLE IF NOT EXISTS deliveries(
    id TEXT PRIMARY KEY, thread TEXT NOT NULL, directory TEXT NOT NULL,
    prompt TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', reply TEXT
);
CREATE TABLE IF NOT EXISTS checks(
    item TEXT NOT NULL REFERENCES items(id), attempt INTEGER NOT NULL,
    revision TEXT NOT NULL, status TEXT NOT NULL, exit_code INTEGER NOT NULL,
    output TEXT NOT NULL, delivery TEXT NOT NULL REFERENCES deliveries(id),
    PRIMARY KEY(item, attempt)
);
CREATE TABLE IF NOT EXISTS worktree_ids(
    item TEXT PRIMARY KEY REFERENCES items(id),
    device TEXT NOT NULL, inode TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transfers(
    id TEXT PRIMARY KEY, item TEXT NOT NULL REFERENCES items(id),
    sender TEXT NOT NULL, requested_thread TEXT, receiver TEXT,
    next_action TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'offered', detail TEXT,
    action_delivery TEXT REFERENCES deliveries(id), action_error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_offer ON transfers(item) WHERE state='offered';
CREATE TABLE IF NOT EXISTS terminal_offers(
    id TEXT PRIMARY KEY REFERENCES transfers(id)
);
CREATE TABLE IF NOT EXISTS recipient_launches(
    transfer TEXT PRIMARY KEY REFERENCES transfers(id),
    host TEXT NOT NULL, prompt TEXT NOT NULL, directory TEXT NOT NULL,
    history TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lane_decisions(
    id TEXT PRIMARY KEY, lane TEXT NOT NULL, model TEXT NOT NULL,
    effort TEXT NOT NULL, reason TEXT NOT NULL, input_hash TEXT NOT NULL, thread TEXT
);
CREATE TABLE IF NOT EXISTS intakes(
    name TEXT PRIMARY KEY REFERENCES items(id),
    task TEXT NOT NULL, delivery TEXT NOT NULL REFERENCES deliveries(id)
);
"""


@contextmanager
def database(path: Path) -> Iterator[sqlite3.Connection]:
    if not path.is_absolute():
        raise TjenticError("The workflow state directory must be absolute.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(path / "workflows.sqlite3")
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(_SCHEMA)
        if not any(
            column["name"] == "id" for column in db.execute("PRAGMA table_info(items)")
        ):
            with db:
                db.execute("BEGIN IMMEDIATE")
                columns = {
                    column["name"] for column in db.execute("PRAGMA table_info(items)")
                }
                if "id" not in columns:
                    db.execute("ALTER TABLE items RENAME COLUMN name TO id")
                    db.execute("ALTER TABLE items ADD COLUMN name TEXT")
                    db.execute("UPDATE items SET name=id")
                    db.execute(
                        "ALTER TABLE items ADD COLUMN host TEXT NOT NULL "
                        "DEFAULT 'codex'"
                    )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS item_name_in_worktree "
            "ON items(worktree,name)"
        )
        yield db
    finally:
        db.close()


@contextmanager
def claim(path: Path, name: str) -> Iterator[None]:
    """Serialize foreground changes to one item. The OS releases this lock on exit."""
    key = hashlib.sha256(name.encode()).hexdigest()
    with (path / f"{key}.lock").open("a+b") as lock:
        try:
            if sys.platform == "win32":
                import msvcrt

                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise TjenticError(f"Workflow item {name!r} is busy.") from error
        yield


@contextmanager
def claim_item(path: Path, root: Path, name: str) -> Iterator[None]:
    with claim(path, json.dumps([str(root), name])):
        yield


def item(db: sqlite3.Connection, identity: str) -> sqlite3.Row:
    row: sqlite3.Row | None = db.execute(
        "SELECT * FROM items WHERE id=?", (identity,)
    ).fetchone()
    if row is None:
        raise TjenticError(f"Unknown workflow item {identity!r}.")
    return row


def find_item(db: sqlite3.Connection, root: Path, name: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = db.execute(
        "SELECT * FROM items WHERE worktree=? AND name=?", (str(root), name)
    ).fetchone()
    return row


def latest_check(db: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = db.execute(
        "SELECT * FROM checks WHERE item=? ORDER BY attempt DESC LIMIT 1", (name,)
    ).fetchone()
    return row
