import argparse
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import UUID, uuid4

from tjentic.errors import TjenticError
from tjentic.sessions import claude
from tjentic.sessions.codex import connect
from tjentic.sessions.hosts import Host
from tjentic.terminal import Kitty
from tjentic.watch.native import Watch, listener_path
from tjentic.workflows.assignment import launch_assignment
from tjentic.workflows.checks import verify_tree, worktree
from tjentic.workflows.delivery import deliver_to
from tjentic.workflows.lanes import bind_launch, record_launch, select
from tjentic.workflows.runner import bind, bind_worktree
from tjentic.workflows.store import (
    claim,
    claim_item,
    database,
    find_item,
    item,
    latest_check,
)


def transfer(db: sqlite3.Connection, identity: str) -> sqlite3.Row:
    row: sqlite3.Row | None = db.execute(
        "SELECT * FROM transfers WHERE id=?", (identity,)
    ).fetchone()
    if row is None:
        raise TjenticError(f"Unknown transfer {identity}.")
    return row


def offer(
    db: sqlite3.Connection,
    row: sqlite3.Row,
    args: argparse.Namespace,
    caller: str | None,
) -> sqlite3.Row:
    target = str(args.to) if args.to else None
    last: sqlite3.Row | None = db.execute(
        "SELECT * FROM transfers WHERE item=? ORDER BY rowid DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    if (
        last
        and last["state"] in {"offered", "accepted"}
        and last["next_action"] == args.next
        and last["requested_thread"] == target
    ) and (caller is None or caller == last["sender"]):
        return last
    if caller is not None and caller != row["thread"]:
        raise TjenticError("This conversation does not own the named item.")
    if last and last["state"] == "offered":
        raise TjenticError(f"Item has an outstanding transfer {last['id']}.")
    if last and last["action_delivery"]:
        action = db.execute(
            "SELECT state FROM deliveries WHERE id=?", (last["action_delivery"],)
        ).fetchone()
        if action["state"] != "done":
            raise TjenticError(
                "The previous recipient's assignment is still outstanding."
            )
    if row["phase"] in {"implement", "deliver", "checking"}:
        raise TjenticError("Resolve the active phase before handing off the item.")
    if target == row["thread"]:
        raise TjenticError("The recipient already owns this item.")
    identity = str(uuid4())
    with db:
        db.execute(
            "INSERT INTO transfers(id,item,sender,requested_thread,next_action) "
            "VALUES(?,?,?,?,?)",
            (identity, row["id"], row["thread"], target, args.next),
        )
    return transfer(db, identity)


async def dispatch(
    db: sqlite3.Connection,
    command: Sequence[str],
    request: sqlite3.Row,
    config: Path,
    profile: Path | None = None,
) -> None:
    if request["state"] != "offered":
        return
    identity = request["id"]
    row = item(db, request["item"])
    root = verify_tree(db, row)
    async with connect(command) as codex:
        delivery = db.execute(
            "SELECT * FROM deliveries WHERE id=?", (identity,)
        ).fetchone()
        opened = None
        lane = None
        if delivery is None or (
            request["requested_thread"] is None and delivery["state"] == "pending"
        ):
            if request["requested_thread"]:
                receiver = await codex.find(
                    UUID(request["requested_thread"]), workflow=True
                )
            else:
                lane = await select(
                    db, identity, command, root, request["next_action"], config
                )
                opened = await codex.create(
                    root, lane.settings.model, effort=lane.settings.effort
                )
                receiver = opened.session
            directory = receiver.directory.resolve()
            if worktree(directory) != root:
                raise TjenticError(
                    "The receiving conversation must be in the item's worktree."
                )
            check = latest_check(db, row["id"])
            summary = json.dumps(dict(check)) if check else "No check yet."
            prompt = (
                f"TJ handoff {identity}\nItem: {row['name']}\nWorktree: {root}\n"
                f"Sender: {request['sender']}\nLast check: {summary}\n"
                f"Next action: {request['next_action']}\n"
                f"To take ownership of this item, reply exactly TJ ACCEPT {identity}. "
                "Otherwise explain why you decline. Wait for acceptance."
            )
            with db:
                if opened is not None and lane is not None:
                    bind_launch(db, identity, lane, opened)
                db.execute(
                    "INSERT INTO deliveries(id,thread,directory,prompt) "
                    "VALUES(?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET thread=excluded.thread, "
                    "directory=excluded.directory",
                    (identity, str(receiver.id), str(directory), prompt),
                )
                db.execute(
                    "UPDATE transfers SET receiver=? WHERE id=?",
                    (str(receiver.id), identity),
                )
        reply = await deliver_to(db, codex, identity, new_thread=opened is not None)
    record_decision(db, request, reply, profile)


def record_decision(
    db: sqlite3.Connection,
    request: sqlite3.Row,
    reply: str,
    profile: Path | None = None,
) -> None:
    identity = request["id"]
    key = request["item"]
    with db:
        if reply.strip() == f"TJ ACCEPT {identity}":
            cursor = db.execute(
                "UPDATE items SET thread=(SELECT thread FROM deliveries WHERE id=?), "
                "directory=(SELECT directory FROM deliveries WHERE id=?) "
                "WHERE id=? AND thread=?",
                (identity, identity, key, request["sender"]),
            )
            if cursor.rowcount != 1:
                raise TjenticError("Item ownership changed before acceptance.")
            rebind_watch(db, key, profile)
            action = str(uuid4())
            owner = item(db, key)
            prompt = (
                f"TJ assignment {action}\nTransfer {identity} accepted. "
                f"You own only item {owner['name']} in {owner['worktree']}.\n"
                f"Carry out this next action: {request['next_action']}\n"
                "Report the result here. This TJ session can hand off an item with "
                "tj handoff ITEM --next ACTION when requested."
            )
            db.execute(
                "INSERT INTO deliveries(id,thread,directory,prompt) VALUES(?,?,?,?)",
                (action, owner["thread"], owner["directory"], prompt),
            )
            db.execute(
                "UPDATE transfers SET action_delivery=? WHERE id=?", (action, identity)
            )
            outcome = "accepted"
        else:
            outcome = "declined"
        db.execute(
            "UPDATE transfers SET state=?, detail=? WHERE id=?",
            (outcome, reply, identity),
        )


async def open_recipient(
    db: sqlite3.Connection,
    state: Path,
    command: Sequence[str],
    request: sqlite3.Row,
    config: Path,
    terminal: Kitty,
    host: Host,
    environment: Mapping[str, str],
) -> None:
    identity = request["id"]
    row = item(db, request["item"])
    root = verify_tree(db, row)
    lane = await select(
        db,
        identity,
        command,
        root,
        request["next_action"],
        config,
        host=host,
        environment=environment,
    )
    name = row["name"]
    assignment = (
        f"TJ handoff {identity}\nItem: {name}\nWorktree: {root}\n\n"
        f"Assignment:\n{request['next_action']}"
    )
    target = request["receiver"] or request["requested_thread"]
    directory = root
    if host is Host.CLAUDE:
        recipient = UUID(target) if target else uuid4()
        history = claude.transcript(root, recipient, environment)
        resume = history.is_file()
        if request["requested_thread"]:
            saved = claude.saved_session(recipient, environment)
            if worktree(saved.directory) != root:
                raise TjenticError("The recipient is in a different worktree.")
            directory, history, resume = saved.directory, saved.transcript, True
        settings = lane.settings
        arguments = [
            *claude.arguments(recipient, resume=resume),
            "--model",
            settings.model,
            "--effort",
            settings.effort,
            "--",
            assignment,
        ]
    else:
        async with connect(command, directory=root, environment=environment) as codex:
            if target:
                receiver = await codex.find(UUID(target), workflow=True)
                directory = receiver.directory.resolve()
                if worktree(directory) != root:
                    raise TjenticError("The recipient is in a different worktree.")
                await codex.acquire(receiver.id, directory)
                model = lane.settings.model
            else:
                opened = await codex.create(
                    root, lane.settings.model, effort=lane.settings.effort
                )
                receiver = opened.session
                # Naming the empty thread makes it durable before the first model turn.
                await codex.name(receiver.id, name[:64])
                model = opened.settings.model
            recipient = receiver.id
            history = await codex.transcript(recipient)
            launch = await codex.terminal_launch(
                directory, model, lane.settings.effort, assignment, recipient
            )
        settings = launch.settings
        arguments = list(launch.arguments)
    with db:
        record_launch(db, identity, lane, settings, str(recipient))
        db.execute(
            "UPDATE transfers SET receiver=?,detail=? WHERE id=?",
            (
                str(recipient),
                "Waiting for the recipient to submit the assignment.",
                identity,
            ),
        )
        db.execute(
            "INSERT OR REPLACE INTO recipient_launches VALUES(?,?,?,?,?)",
            (identity, host.value, assignment, str(directory), str(history)),
        )
    watch = Watch(host, state, host.profile(environment), listener_path())
    options = (
        watch.claude_config(directory, recipient)
        if host is Host.CLAUDE
        else watch.codex_config(directory)
    )
    try:
        terminal.open_tab(
            directory,
            name[:64],
            [
                sys.executable,
                "-m",
                "tjentic.workflows.recipient",
                str(state),
                identity,
                *command,
                *options,
                *arguments,
            ],
            identity,
        )
    except subprocess.TimeoutExpired as error:
        raise TjenticError(
            "Kitty did not confirm the recipient tab. Check it before retrying."
        ) from error
    except (OSError, TjenticError):
        with db:
            db.execute("DELETE FROM recipient_launches WHERE transfer=?", (identity,))
        raise


def rebind_watch(db: sqlite3.Connection, key: str, profile: Path | None) -> None:
    if (
        profile is None
        or not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='pr_waits'"
        ).fetchone()
    ):
        return
    db.execute(
        "UPDATE pr_waits SET thread=i.thread,host=i.host,profile=?,state='pending' "
        "FROM items i WHERE i.id=pr_waits.item AND i.id=? AND state='rebind'",
        (str(profile), key),
    )


def assign_recipient(
    db: sqlite3.Connection,
    request: sqlite3.Row,
    receiver: str,
    directory: Path,
    host: Host,
    profile: Path | None = None,
) -> None:
    identity = request["id"]
    cursor = db.execute(
        "UPDATE items SET thread=?, directory=?, host=? WHERE id=? AND thread=?",
        (receiver, str(directory), host.value, request["item"], request["sender"]),
    )
    if cursor.rowcount != 1:
        raise TjenticError("Item ownership changed before acceptance.")
    rebind_watch(db, request["item"], profile)
    decided = db.execute(
        "UPDATE transfers SET receiver=?,state='accepted',detail=? "
        "WHERE id=? AND state='offered'",
        (receiver, "Recipient received the assignment.", identity),
    )
    if decided.rowcount != 1:
        raise TjenticError("This assignment is no longer open.")
    db.execute("UPDATE lane_decisions SET thread=? WHERE id=?", (receiver, identity))


async def accept_recipient(
    db: sqlite3.Connection,
    command: Sequence[str],
    request: sqlite3.Row,
    caller: str | None,
    ticket: str | None,
    profile: Path | None = None,
) -> None:
    identity = request["id"]
    if (
        not caller
        or ticket != identity
        or caller == request["sender"]
        or request["requested_thread"] not in {None, caller}
    ):
        raise TjenticError("Accept the assignment from its recipient tab.")
    if request["state"] == "accepted" and request["receiver"] == caller:
        return
    if (
        request["state"] != "offered"
        or not db.execute(
            "SELECT 1 FROM terminal_offers WHERE id=?", (identity,)
        ).fetchone()
    ):
        raise TjenticError("This assignment is no longer open.")
    row = item(db, request["item"])
    root = verify_tree(db, row)
    async with connect(command, directory=root) as codex:
        receiver = await codex.find(UUID(caller), workflow=True)
    directory = receiver.directory.resolve()
    if worktree(directory) != root:
        raise TjenticError("The recipient is in a different worktree.")
    with db:
        assign_recipient(db, request, str(receiver.id), directory, Host.CODEX, profile)


async def handoff(
    args: argparse.Namespace,
    command: Sequence[str] | None,
    state: Path,
    caller: str | None,
    config: Path,
    terminal: Kitty | None,
    ticket: str | None,
    host: Host = Host.CODEX,
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    if not (args.retry or args.cancel or args.accept) and terminal is None:
        raise TjenticError("Run `tj handoff` in a Kitty terminal.")
    with database(state) as db:
        saved = args.retry or args.cancel or args.accept
        if saved:
            owner = item(db, transfer(db, str(saved))["item"])
            name, root = owner["name"], Path(owner["worktree"])
            saved_host = Host(owner["host"])
            if caller is not None and saved_host is not host and not args.accept:
                raise TjenticError("The originating session uses a different provider.")
            if saved_host is not host:
                host, command = saved_host, None
        else:
            name, root = args.item, worktree(Path.cwd())
        with claim_item(state, root, name):
            if saved:
                request = transfer(db, str(saved))
                if args.accept:
                    await accept_recipient(
                        db,
                        command or Host.CODEX.command(),
                        request,
                        caller,
                        ticket,
                        Host.CODEX.profile(environment or {}),
                    )
                    print(f"Accepted {name}.\n\n{request['next_action']}")
                    return 0
                if caller and caller != request["sender"]:
                    raise TjenticError(
                        "Only the originating conversation can retry this transfer."
                    )
            else:
                existing = find_item(db, root, name)
                if existing is None:
                    if caller and host is Host.CODEX:
                        existing = await bind(
                            db, name, UUID(caller), [], command or host.command()
                        )
                    else:
                        directory = Path.cwd().resolve()
                        if caller:
                            sender = claude.saved_session(
                                UUID(caller), environment or {}
                            )
                            if worktree(sender.directory) != root:
                                raise TjenticError(
                                    "The sending session is in a different worktree."
                                )
                            directory = sender.directory
                        existing = bind_worktree(
                            db,
                            name,
                            directory,
                            thread=caller or "",
                            host=host,
                        )
                elif host != existing["host"]:
                    raise TjenticError("This item belongs to a different provider.")
                request = offer(db, existing, args, caller)
            identity = request["id"]
            if args.cancel:
                sent = db.execute(
                    "SELECT state FROM deliveries WHERE id=?", (identity,)
                ).fetchone()
                if request["state"] != "offered" or (
                    sent and sent["state"] != "pending"
                ):
                    raise TjenticError("Only an unsent offer can be cancelled.")
                with claim(state, "recipient/" + identity), db:
                    db.execute(
                        "UPDATE transfers SET state='cancelled' WHERE id=?", (identity,)
                    )
                print(json.dumps(dict(transfer(db, identity))))
                return 0
            print(f"TJ transfer {identity}", file=sys.stderr, flush=True)
            try:
                legacy_tab = db.execute(
                    "SELECT 1 FROM terminal_offers WHERE id=?", (identity,)
                ).fetchone()
                if request["state"] == "offered":
                    delivery = db.execute(
                        "SELECT 1 FROM deliveries WHERE id=?", (identity,)
                    ).fetchone()
                    if delivery:
                        await dispatch(
                            db,
                            command or host.command(),
                            request,
                            config,
                            host.profile(environment or {}),
                        )
                    elif legacy_tab:
                        print(f"The older recipient tab is still open for {name}.")
                        return 0
                    else:
                        queued = db.execute(
                            "SELECT 1 FROM recipient_launches WHERE transfer=?",
                            (identity,),
                        ).fetchone()
                        if queued and not args.retry:
                            print(
                                f"Waiting for the recipient of {name} "
                                "to submit the assignment."
                            )
                            return 0
                        with claim(state, "recipient/" + identity):
                            pass
                        if terminal is None:
                            raise TjenticError("Run `tj handoff` in a Kitty terminal.")
                        await open_recipient(
                            db,
                            state,
                            command or host.command(),
                            request,
                            config,
                            terminal,
                            host,
                            os.environ if environment is None else environment,
                        )
                        print(
                            f"Opened the recipient tab for {name}. "
                            "Waiting for the assignment to reach it."
                        )
                        return 0
            except (TjenticError, OSError, ValueError) as error:
                with db:
                    db.execute(
                        "UPDATE transfers SET detail=? WHERE id=?",
                        (str(error), identity),
                    )
                raise TjenticError(str(error)) from error
            result = transfer(db, identity)
            if (
                result["state"] == "accepted"
                and result["action_delivery"]
                and not item(db, result["item"])["hold"]
            ):
                launch_assignment(db, state, result, command or host.command())
            receipt = dict(result)
            receipt["item"] = name
            lane = db.execute(
                "SELECT lane,model,effort,reason FROM lane_decisions WHERE id=?",
                (identity,),
            ).fetchone()
            if lane:
                receipt["lane"] = dict(lane)
            print(json.dumps(receipt))
            return 0 if result["state"] == "accepted" else 1
