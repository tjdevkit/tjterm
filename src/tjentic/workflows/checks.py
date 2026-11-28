import json
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

from tjentic.errors import TjenticError
from tjentic.workflows.store import latest_check


def git(directory: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(directory), *args], capture_output=True, check=False
    )
    if result.returncode:
        raise TjenticError(f"Could not read Git worktree at {directory}.")
    return result.stdout.decode(errors="replace").rstrip("\n")


def worktree(directory: Path) -> Path:
    return Path(git(directory, "rev-parse", "--show-toplevel")).resolve()


def verify_tree(db: sqlite3.Connection, row: sqlite3.Row) -> Path:
    root = Path(row["worktree"])
    if (
        worktree(root) != root
        or git(root, "rev-parse", "--absolute-git-dir") != row["git_dir"]
    ):
        raise TjenticError(f"The registered Git worktree at {root} changed.")
    identity = db.execute(
        "SELECT * FROM worktree_ids WHERE item=?", (row["id"],)
    ).fetchone()
    if identity:
        metadata = Path(row["git_dir"]).stat()
        if (str(metadata.st_dev), str(metadata.st_ino)) != (
            identity["device"],
            identity["inode"],
        ):
            raise TjenticError(f"The registered Git directory for {root} was replaced.")
    return root


def run_check(db: sqlite3.Connection, row: sqlite3.Row) -> None:
    key = row["id"]
    root = verify_tree(db, row)
    revision = git(root, "rev-parse", "--verify", "HEAD")
    status = git(root, "status", "--porcelain=v1", "--untracked-files=all")
    previous = latest_check(db, key)
    attempt = previous["attempt"] + 1 if previous else 1
    with db:
        db.execute("UPDATE items SET phase='checking' WHERE id=?", (key,))
    with tempfile.TemporaryFile() as log:
        result = subprocess.run(
            json.loads(row["command"]),
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
        size = log.tell()
        log.seek(max(0, size - 16384))
        output = log.read().decode(errors="replace")
        if size > 16384:
            output = "[earlier output omitted]\n" + output
    delivery = str(uuid4())
    action = (
        (
            "Repair this failure in the worktree and reply exactly "
            f"TJ RECHECK {delivery} "
            "when ready, or explain what requires developer input."
        )
        if result.returncode
        else ("This item is ready for local review. Acknowledge receipt and stop.")
    )
    prompt = (
        f"TJ check {delivery}\nItem: {row['name']}\nWorktree: {root}\n"
        f"Attempt {attempt}, revision {revision}, exit {result.returncode}\n"
        f"Check command: {row['command']}\nCheck output:\n{output}\n\n{action}"
    )
    with db:
        db.execute(
            "INSERT INTO deliveries(id,thread,directory,prompt) VALUES(?,?,?,?)",
            (delivery, row["thread"], row["directory"], prompt),
        )
        db.execute(
            "INSERT INTO checks VALUES(?,?,?,?,?,?,?)",
            (key, attempt, revision, status, result.returncode, output, delivery),
        )
        db.execute("UPDATE items SET phase='deliver' WHERE id=?", (key,))
