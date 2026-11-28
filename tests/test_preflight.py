import asyncio
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from uuid import UUID

import pytest

from tjentic.errors import TjenticError
from tjentic.watch.preflight import verify


@pytest.mark.allow_hosts(["127.0.0.1"])
def test_offline_check_validates_the_exact_thread_directory_and_worktree_binding(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    child = root / "nested"
    child.mkdir()
    profile = tmp_path / "profile"
    owner = UUID("019a0000-0000-7000-8000-000000000001")
    metadata = root / ".git"
    stat = metadata.stat()
    with closing(sqlite3.connect(tmp_path / "workflows.sqlite3")) as db, db:
        db.executescript(
            "CREATE TABLE items (id, worktree, git_dir);"
            "CREATE TABLE worktree_ids (item, device, inode);"
        )
        db.execute(
            "INSERT INTO items VALUES('widget-id',?,?)", (str(root), str(metadata))
        )
        db.execute(
            "INSERT INTO worktree_ids VALUES('widget-id',?,?)",
            (str(stat.st_dev), str(stat.st_ino)),
        )
    response = tmp_path / "scenario.json"
    response.write_text(
        json.dumps({"thread": {"id": str(owner), "cwd": str(child), "updatedAt": 1}}),
        encoding="utf-8",
    )
    command = [
        sys.executable,
        str(Path(__file__).parent / "fixtures/codex.py"),
        str(response),
    ]
    environment = {"CODEX_HOME": str(profile)}
    asyncio.run(verify(tmp_path, "widget-id", owner, command, environment))
    (root / ".tjentic").mkdir()
    (root / ".tjentic/standards.toml").write_text("standards = []", encoding="utf-8")
    with pytest.raises(TjenticError, match="Migrate to native SessionStart"):
        asyncio.run(verify(tmp_path, "widget-id", owner, command, environment))
    unrelated = tmp_path / "unrelated"
    subprocess.run(["git", "init", "-q", str(unrelated)], check=True)
    response.write_text(
        json.dumps(
            {"thread": {"id": str(owner), "cwd": str(unrelated), "updatedAt": 1}}
        ),
        encoding="utf-8",
    )
    with pytest.raises(TjenticError, match="registered worktree"):
        asyncio.run(verify(tmp_path, "widget-id", owner, command, environment))
    requests = [
        json.loads(line)["method"]
        for line in response.with_suffix(".log")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert requests == ["initialize", "initialized", "thread/read"] * 3
