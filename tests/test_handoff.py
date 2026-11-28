import argparse
import asyncio
import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from tjentic.errors import TjenticError
from tjentic.sessions.codex import (
    Codex,
    Model,
    ModelSettings,
    OpenedSession,
    Session,
    TerminalLaunch,
)
from tjentic.terminal import Kitty
from tjentic.workflows.handoff import dispatch, handoff, offer, transfer
from tjentic.workflows.recipient import run as run_recipient
from tjentic.workflows.store import database, find_item, item

SENDER = "019a0000-0000-7000-8000-000000000001"
RECEIVER = UUID("019a0000-0000-7000-8000-000000000002")


@pytest.mark.allow_hosts(["127.0.0.1"])
@pytest.mark.parametrize("fail_first", [False, True])
def test_handoff_assigns_once_and_opens_the_recipient_with_the_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fail_first: bool,
) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.chdir(repo)
    config = tmp_path / "lanes.toml"
    config.write_text(
        "".join(f'[codex.L{i}]\neffort="low"\n' for i in range(1, 7)), encoding="utf-8"
    )
    state = tmp_path / "state"
    host = AsyncMock(spec=Codex)
    host.assess.return_value = '{"kind":"investigation","reason":"Inspect parser."}'
    session = Session(RECEIVER, repo, "", datetime(2026, 1, 1, tzinfo=UTC))
    host.create.return_value = OpenedSession(
        session, ModelSettings("gpt-6.2-sol", "low")
    )
    host.find.return_value = session
    history = tmp_path / "codex.jsonl"
    host.transcript.return_value = history
    host.terminal_launch.return_value = TerminalLaunch(
        ("resume", str(RECEIVER)), ModelSettings("gpt-6.2-sol", "low")
    )
    connection = MagicMock()
    connection.return_value.__aenter__.return_value = host
    monkeypatch.setattr("tjentic.workflows.handoff.connect", connection)
    monkeypatch.setattr("tjentic.workflows.lanes.codex.connect", connection)
    terminal = MagicMock(spec=Kitty)
    task = "Review the parser change"
    args = argparse.Namespace(
        item=task, next=task, accept=None, retry=None, cancel=None, to=None
    )

    async def run() -> None:
        if fail_first:
            terminal.open_tab.side_effect = TjenticError("Kitty launch failed.")
            with pytest.raises(TjenticError, match="Kitty launch failed"):
                await handoff(args, ["codex"], state, None, config, terminal, None)
            with database(state) as db:
                pending = db.execute("SELECT * FROM transfers").fetchone()
                assert pending["state"] == "offered"
                assert pending["receiver"] == str(RECEIVER)
                assert not item(db, pending["item"])["thread"]
            terminal.open_tab.side_effect = None
        for _ in range(2):
            assert (
                await handoff(args, ["codex"], state, None, config, terminal, None) == 0
            )

    asyncio.run(run())
    assert terminal.open_tab.call_count == (2 if fail_first else 1)
    command = terminal.open_tab.call_args.args[2]
    assert command[2] == "tjentic.workflows.recipient"
    assert command[5:] == ["codex", "resume", str(RECEIVER)]
    host.create.assert_awaited_once()
    host.name.assert_awaited_once_with(RECEIVER, task)
    prompt = host.terminal_launch.await_args.args[3]
    assert prompt.endswith(f"Assignment:\n{task}")
    assert "--accept" not in prompt
    host.send.assert_not_awaited()
    assert task in capsys.readouterr().out
    identity = command[4]
    with database(state) as db:
        pending = transfer(db, identity)
        assert pending["state"] == "offered"
        assert not item(db, pending["item"])["thread"]
    with pytest.raises(TjenticError, match="closed before submitting"):
        run_recipient(
            state, identity, [sys.executable, "-c", "raise SystemExit(1)"], {}
        )
    with database(state) as db:
        pending = transfer(db, identity)
        assert pending["state"] == "offered"
        assert not item(db, pending["item"])["thread"]
    assert (
        asyncio.run(handoff(args, ["codex"], state, None, config, terminal, None)) == 0
    )
    message = {
        "type": "response_item",
        "payload": {
            "role": "user",
            "content": [{"type": "input_text", "text": prompt}],
        },
    }
    script = (
        "from pathlib import Path; import sys; "
        "Path(sys.argv[1]).write_text(sys.argv[2] + '\\n', encoding='utf-8')"
    )
    assert (
        run_recipient(
            state,
            identity,
            [sys.executable, "-c", script, str(history), json.dumps(message)],
            {},
        )
        == 0
    )
    with database(state) as db:
        saved = transfer(db, identity)
        assert saved["state"] == "accepted"
        assert item(db, saved["item"])["thread"] == str(RECEIVER)
    another = tmp_path / "other"
    subprocess.run(["git", "init", "-q", str(another)], check=True)
    monkeypatch.chdir(another)
    assert (
        asyncio.run(handoff(args, ["codex"], state, None, config, terminal, None)) == 0
    )
    with database(state) as db:
        separate = find_item(db, another.resolve(), task)
        assert separate is not None
        assert separate["id"] != saved["item"]
        assert not separate["thread"]
    assert terminal.open_tab.call_args.args[0] == another.resolve()


# asyncio uses a local socket pair to wake its event loop.
@pytest.mark.allow_hosts(["127.0.0.1"])
@pytest.mark.parametrize(
    ("accept", "kind", "family", "model", "rung"),
    [
        (True, "critical-design", Model.ASTRA, "gpt-6.2-astra", "L1"),
        (False, "implementation", Model.TERRA, "gpt-6.1-terra", "L4"),
    ],
)
def test_retry_reconciles_the_receiver_reply_and_only_moves_one_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    accept: bool,
    kind: str,
    family: Model,
    model: str,
    rung: str,
) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    host = AsyncMock(spec=Codex)
    selected = ModelSettings(model, "low")
    host.create.return_value = OpenedSession(
        Session(RECEIVER, repo, "", datetime(2026, 1, 1, tzinfo=UTC)), selected
    )
    connection = MagicMock()
    connection.return_value.__aenter__.return_value = host
    monkeypatch.setattr("tjentic.workflows.handoff.connect", connection)
    monkeypatch.setattr("tjentic.workflows.lanes.codex.connect", connection)
    config = tmp_path / "tjentic"
    config.mkdir()
    presets = config / "lanes.toml"
    presets.write_text(
        "".join(f'[codex.L{lane}]\neffort="low"\n' for lane in range(1, 7)),
        encoding="utf-8",
    )
    host.assess.return_value = f'{{"kind":"{kind}","reason":"Specific scope."}}'
    args = argparse.Namespace(to=None, next="Review the repaired file.")
    with database(tmp_path / "state") as db:
        with db:
            for name in ("X", "Y"):
                db.execute(
                    "INSERT INTO items(id,name,thread,directory,worktree,git_dir,"
                    "command,phase) "
                    "VALUES(?,?,?,?,?,?,'[]','assigned')",
                    (name, name, SENDER, str(repo), str(repo), str(repo / ".git")),
                )
        request = offer(db, item(db, "X"), args, SENDER)
        identity = request["id"]
        reply = f"TJ ACCEPT {identity}" if accept else "Need the review criteria first."
        host.received.return_value = reply
        db.execute("""
            CREATE TRIGGER cut_power BEFORE UPDATE OF state ON deliveries
            WHEN NEW.state='done' BEGIN SELECT RAISE(ABORT,'interrupt receipt'); END
        """)
        with pytest.raises(sqlite3.IntegrityError, match="interrupt receipt"):
            asyncio.run(dispatch(db, ["codex"], request, presets))
        assert item(db, "X")["thread"] == SENDER
        assert transfer(db, identity)["state"] == "offered"
        assert transfer(db, identity)["receiver"] == str(RECEIVER)

        presets.unlink()
        db.execute("DROP TRIGGER cut_power")
        for _ in range(2):
            asyncio.run(dispatch(db, ["codex"], transfer(db, identity), presets))
        host.assess.assert_awaited_once()
        assert host.assess.await_args.args[-1] is Model.LUNA
        assert host.assess.await_args.kwargs["effort"] == "low"
        host.create.assert_awaited_once_with(repo, family, effort="low")
        route = db.execute(
            "SELECT lane,model FROM lane_decisions WHERE id=?", (identity,)
        ).fetchone()
        assert route["lane"] == rung and route["model"] == model
        offer_delivery = db.execute(
            "SELECT prompt FROM deliveries WHERE id=?", (identity,)
        ).fetchone()
        host.send.assert_awaited_once_with(
            RECEIVER, identity, offer_delivery["prompt"], selected
        )
        host.acquire.assert_awaited_once_with(RECEIVER, repo)
        final = transfer(db, identity)
        assert final["state"] == ("accepted" if accept else "declined")
        assert bool(final["action_delivery"]) is accept
        assert item(db, "X")["thread"] == (str(RECEIVER) if accept else SENDER)
        assert item(db, "Y")["thread"] == SENDER
        if accept:
            assert offer(db, item(db, "X"), args, SENDER)["id"] == identity
            with pytest.raises(TjenticError, match="does not own"):
                offer(
                    db,
                    item(db, "X"),
                    argparse.Namespace(to=None, next="Different"),
                    SENDER,
                )
