import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import UUID

from tjentic.errors import TjenticError
from tjentic.sessions.codex import Codex, ModelSettings, connect


async def deliver(
    db: sqlite3.Connection,
    command: Sequence[str],
    delivery: str,
    *,
    environment: Mapping[str, str],
) -> str:
    async with connect(command, environment=environment) as codex:
        return await deliver_to(db, codex, delivery)


async def deliver_to(
    db: sqlite3.Connection, codex: Codex, delivery: str, *, new_thread: bool = False
) -> str:
    row = db.execute("SELECT * FROM deliveries WHERE id=?", (delivery,)).fetchone()
    assert row is not None
    if row["state"] == "done":
        return str(row["reply"])
    identity = UUID(row["thread"])
    if not new_thread:
        await codex.acquire(identity, Path(row["directory"]))
    if row["state"] == "dispatching":
        reply = await codex.received(identity, row["prompt"])
        if reply is None:
            raise TjenticError(
                "Delivery unconfirmed in native history. Inspect the bound thread."
            )
    else:
        with db:
            db.execute(
                "UPDATE deliveries SET state='dispatching' WHERE id=?", (delivery,)
            )
        decision = db.execute(
            "SELECT model,effort FROM lane_decisions "
            "WHERE thread=? ORDER BY rowid DESC LIMIT 1",
            (row["thread"],),
        ).fetchone()
        settings = (
            ModelSettings(decision["model"], decision["effort"]) if decision else None
        )
        await codex.send(identity, delivery, row["prompt"], settings)
        reply = await codex.received(identity, row["prompt"])
        if reply is None:
            raise TjenticError("Codex completed without a matching delivery receipt.")
    with db:
        db.execute(
            "UPDATE deliveries SET state='done', reply=? WHERE id=?",
            (reply, delivery),
        )
    return reply
