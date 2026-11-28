import sqlite3
from pathlib import Path

import pytest

from tjentic.errors import TjenticError
from tjentic.workflows.store import claim, database, find_item


def test_claim_releases_item_without_blocking_independent_work(tmp_path: Path) -> None:
    with (
        claim(tmp_path, "widgets"),
        claim(tmp_path, "other"),
        pytest.raises(TjenticError, match="busy"),
        claim(tmp_path, "widgets"),
    ):
        pytest.fail("same item had two foreground workers")
    with claim(tmp_path, "widgets"):
        pass


def test_database_upgrades_existing_items_without_losing_transfer_links(
    tmp_path: Path,
) -> None:
    filename = tmp_path / "workflows.sqlite3"
    with sqlite3.connect(filename) as old:
        old.executescript("""
            CREATE TABLE items(
                name TEXT PRIMARY KEY, thread TEXT NOT NULL, directory TEXT NOT NULL,
                worktree TEXT NOT NULL, git_dir TEXT NOT NULL, command TEXT NOT NULL,
                phase TEXT NOT NULL, hold TEXT, last_error TEXT
            );
            CREATE TABLE old_link(
                transfer TEXT PRIMARY KEY, item REFERENCES items(name)
            );
        """)
        old.execute(
            "INSERT INTO items VALUES('task','owner',? ,?,?,'[]','assigned',NULL,NULL)",
            (str(tmp_path), str(tmp_path), str(tmp_path / ".git")),
        )
        old.execute("INSERT INTO old_link VALUES('saved','task')")
    old.close()
    with database(tmp_path) as current:
        row = find_item(current, tmp_path, "task")
        assert row is not None
        assert row["id"] == "task" and row["thread"] == "owner"
        assert current.execute("SELECT item FROM old_link").fetchone()["item"] == "task"
        assert not current.execute("PRAGMA foreign_key_check").fetchall()
