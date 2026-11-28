"""Validate an addressed Codex wake before the listener queues its first input."""

import argparse
import asyncio
import os
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path
from uuid import UUID

from tjentic import standards
from tjentic.errors import TjenticError
from tjentic.hosts.host import Host
from tjentic.sessions.codex import Session, connect
from tjentic.workflows import store
from tjentic.workflows.checks import verify_tree, worktree


async def verify(
    state: Path,
    item_key: str,
    owner: UUID,
    command: Sequence[str],
    environment: Mapping[str, str],
) -> None:
    database = (state / "workflows.sqlite3").as_uri() + "?mode=ro"
    with closing(sqlite3.connect(database, uri=True)) as db:
        db.row_factory = sqlite3.Row
        root = verify_tree(db, store.item(db, item_key))
    async with connect(command, directory=root, environment=environment) as codex:
        session = Session.from_thread(await codex.read(owner))
    directory = session.directory.resolve(strict=True)
    if not directory.is_dir() or worktree(directory) != root:
        raise TjenticError(
            "The Codex thread does not belong to the registered worktree."
        )
    standards.verify(Host.CODEX, directory, environment)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state", type=Path)
    parser.add_argument("item_key")
    parser.add_argument("owner", type=UUID)
    args = parser.parse_args()
    try:
        asyncio.run(
            verify(
                args.state, args.item_key, args.owner, Host.CODEX.command(), os.environ
            )
        )
    except (OSError, ValueError, sqlite3.Error, TjenticError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
