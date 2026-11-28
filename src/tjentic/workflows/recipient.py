"""Run the recipient in its own terminal and reconcile its first native prompt."""

import os
import sqlite3
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from tjentic import standards
from tjentic.errors import TjenticError
from tjentic.hosts.host import Host
from tjentic.listener import listener_path
from tjentic.sessions.history import SubmittedPrompt
from tjentic.watch.native import Watch
from tjentic.workflows.checks import verify_tree, worktree
from tjentic.workflows.handoff import assign_recipient, transfer
from tjentic.workflows.store import claim, database, item


def run(
    state: Path, identity: str, command: Sequence[str], environment: Mapping[str, str]
) -> int:
    with database(state) as db, claim(state, "recipient/" + identity):
        request = transfer(db, identity)
        row = db.execute(
            "SELECT * FROM recipient_launches WHERE transfer=?", (identity,)
        ).fetchone()
        if request["state"] != "offered" or row is None:
            raise TjenticError("This handoff is no longer waiting for a recipient.")
        directory = Path(row["directory"])
        if worktree(directory) != verify_tree(db, item(db, request["item"])):
            raise TjenticError("The recipient is in a different worktree.")
        provider = Host(row["host"])
        prompt = SubmittedPrompt(
            provider, Path(row["history"]), request["receiver"], row["prompt"]
        )
        env = dict(environment)
        for name in (
            "CLAUDECODE",
            Host.CLAUDE.session_variable,
            Host.CODEX.session_variable,
        ):
            env.pop(name, None)
        env["TJ_HOST"] = provider.value
        if provider is Host.CLAUDE:
            env[provider.session_variable] = request["receiver"]
        try:
            standards.verify(provider, directory, env)
            submitted = prompt.visible()
            # A previous launch can leave a valid receipt before TJ could record it.
            argv = command[:-2] if submitted else command
            watch = Watch(
                provider, state, provider.profile(environment), listener_path()
            )
            watch.call("ensure")
            with subprocess.Popen(argv, cwd=directory, env=env) as child:
                while not submitted:
                    try:
                        child.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        submitted = prompt.visible()
                        continue
                    submitted = prompt.visible()
                    if not submitted:
                        raise TjenticError(
                            "The recipient closed before submitting the assignment. "
                            "Retry the handoff from the sender."
                        )
                with db:
                    assign_recipient(
                        db,
                        request,
                        request["receiver"],
                        directory,
                        provider,
                        watch.profile,
                    )
                return child.wait()
        except (OSError, TjenticError) as error:
            with db:
                db.execute(
                    "UPDATE transfers SET detail=? WHERE id=? AND state='offered'",
                    (str(error), identity),
                )
                db.execute(
                    "DELETE FROM recipient_launches WHERE transfer=? AND EXISTS "
                    "(SELECT 1 FROM transfers WHERE id=? AND state='offered')",
                    (identity, identity),
                )
            raise


def main() -> int:
    state, identity, *command = sys.argv[1:]
    try:
        result = run(Path(state), identity, command, os.environ)
    except (OSError, TjenticError, sqlite3.Error) as error:
        print(f"tj: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return result if result >= 0 else 128 - result


if __name__ == "__main__":
    raise SystemExit(main())
