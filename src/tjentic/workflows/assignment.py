"""Run the accepted task in its receiver without occupying the sender's terminal."""

import asyncio
import os
import sqlite3
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from tjentic.errors import TjenticError
from tjentic.workflows.checks import verify_tree
from tjentic.workflows.delivery import deliver
from tjentic.workflows.store import claim, database, item


def launch_assignment(
    db: sqlite3.Connection,
    state: Path,
    row: sqlite3.Row,
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
) -> None:
    action = db.execute(
        "SELECT state FROM deliveries WHERE id=?", (row["action_delivery"],)
    ).fetchone()
    if action["state"] == "done":
        return
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tjentic.workflows.assignment",
            str(state),
            row["id"],
            *command,
        ],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=sys.platform != "win32",
    )


async def execute(
    state: Path,
    identity: str,
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
) -> None:
    with database(state) as db:
        row = db.execute(
            "SELECT * FROM transfers WHERE id=? AND state='accepted'", (identity,)
        ).fetchone()
        if row is None:
            raise TjenticError("No accepted assignment.")
        with claim(state, "assignment/" + identity):
            owner = item(db, row["item"])
            if owner["thread"] != row["receiver"]:
                raise TjenticError("Assignment no longer owns the item.")
            try:
                if owner["hold"]:
                    return
                verify_tree(db, owner)
                await deliver(
                    db, command, row["action_delivery"], environment=environment
                )
            except (TjenticError, OSError, ValueError) as error:
                with db:
                    db.execute(
                        "UPDATE transfers SET action_error=? WHERE id=?",
                        (str(error), identity),
                    )
                raise
            with db:
                db.execute(
                    "UPDATE transfers SET action_error=NULL WHERE id=?", (identity,)
                )


def main() -> int:
    state, identity, *command = sys.argv[1:]
    try:
        asyncio.run(execute(Path(state), identity, command, environment=os.environ))
    except (TjenticError, OSError, ValueError, sqlite3.Error):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
