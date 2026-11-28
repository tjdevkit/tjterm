import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from tjentic.errors import TjenticError
from tjentic.locking import exclusive

_SCHEMA = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")


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
                    upgrade = (
                        Path(__file__)
                        .with_name("item_ids.sql")
                        .read_text(encoding="utf-8")
                    )
                    for statement in upgrade.splitlines():
                        db.execute(statement)
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
    with exclusive(
        path / f"{key}.lock", busy_message=f"Workflow item {name!r} is busy."
    ):
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
