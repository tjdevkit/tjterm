import argparse
import json
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import UUID, uuid4

from tjentic.errors import TjenticError
from tjentic.listener import Worktree
from tjentic.sessions.codex import connect
from tjentic.workflows.assignment import launch_assignment
from tjentic.workflows.checks import run_check, verify_tree, worktree
from tjentic.workflows.delivery import deliver, deliver_to
from tjentic.workflows.lanes import bind_launch, recorded, select
from tjentic.workflows.store import claim_item, database, find_item, item, latest_check


async def bind(
    db: sqlite3.Connection,
    name: str,
    thread: UUID,
    check: list[str],
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
) -> sqlite3.Row:
    async with connect(command, environment=environment) as codex:
        session = await codex.find(thread, workflow=True)
    directory = session.directory.resolve()
    tree = Worktree.discover(directory)
    if worktree(Path.cwd()) != tree.root:
        raise TjenticError("Start the item from its Codex session's worktree.")
    with db:
        return _bind(
            db,
            name,
            str(session.id),
            directory,
            tree,
            check,
            "check" if check else "assigned",
        )


def bind_worktree(
    db: sqlite3.Connection,
    name: str,
    directory: Path,
    *,
    thread: str = "",
    host: str = "codex",
) -> sqlite3.Row:
    tree = Worktree.discover(directory)
    with db:
        return _bind(db, name, thread, directory, tree, [], "assigned", host=host)


def _bind(
    db: sqlite3.Connection,
    name: str,
    thread: str,
    directory: Path,
    tree: Worktree,
    check: list[str],
    phase: str,
    *,
    host: str = "codex",
) -> sqlite3.Row:
    identity = str(uuid4())
    cursor = db.execute(
        "INSERT INTO items(id,name,thread,directory,worktree,git_dir,"
        "command,phase,host) "
        "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(worktree,name) DO NOTHING",
        (
            identity,
            name,
            thread,
            str(directory),
            str(tree.root),
            str(tree.git_dir),
            json.dumps(check),
            phase,
            host,
        ),
    )
    if not cursor.rowcount:
        raise TjenticError(f"Item {name!r} already exists. Use workflow resume.")
    db.execute(
        "INSERT INTO worktree_ids VALUES(?,?,?)",
        (identity, tree.device, tree.inode),
    )
    return item(db, identity)


async def _begin(
    db: sqlite3.Connection,
    args: argparse.Namespace,
    command: Sequence[str],
    config: Path,
    *,
    environment: Mapping[str, str],
) -> sqlite3.Row:
    name = args.item
    tree = Worktree.discover(Path.cwd())
    root = tree.root
    if find_item(db, root, name) is not None:
        raise TjenticError(f"Item {name!r} already exists. Use workflow resume.")
    lane = await select(
        db, "", command, root, args.task, config, environment=environment
    )
    delivery = str(uuid4())
    prompt = (
        f"TJ implementation {delivery}\nItem: {name}\nWorktree: {root}\n"
        f"Assignment:\n{args.task}\n\n"
        "Implement the authorized task here. This workflow ends at local review. "
        "TJ runs the configured check after implementation. When the change is ready "
        f"reply exactly TJ IMPLEMENTED {delivery}. "
        "If blocked, explain what needs input."
    )
    async with connect(command, environment=environment) as codex:
        opened = await codex.create(
            root, lane.settings.model, effort=lane.settings.effort
        )
        session = opened.session
        thread = str(session.id)
        if Worktree.discover(root) != tree:
            raise TjenticError(f"The registered Git worktree at {root} changed.")
        with db:
            row = _bind(db, name, thread, root, tree, args.check, "implement")
            db.execute(
                "INSERT INTO deliveries(id,thread,directory,prompt) VALUES(?,?,?,?)",
                (delivery, thread, str(root), prompt),
            )
            db.execute(
                "INSERT INTO intakes VALUES(?,?,?)", (row["id"], args.task, delivery)
            )
            bind_launch(db, "item:" + row["id"], lane, opened)
        await deliver_to(db, codex, delivery, new_thread=True)
    return row


async def _implemented(
    db: sqlite3.Connection,
    row: sqlite3.Row,
    root: Path,
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
) -> None:
    name = row["id"]
    delivery = db.execute(
        "SELECT d.id,d.state FROM deliveries d "
        "JOIN intakes i ON i.delivery=d.id WHERE i.name=?",
        (name,),
    ).fetchone()
    identity = delivery["id"]
    if delivery["state"] == "pending":
        # No turn was submitted and Codex may never have persisted that empty thread.
        key = "item:" + name
        lane = recorded(db, key)
        async with connect(command, environment=environment) as codex:
            opened = await codex.create(
                root, lane.settings.model, effort=lane.settings.effort
            )
            thread = str(opened.session.id)
            with db:
                db.execute("UPDATE items SET thread=? WHERE id=?", (thread, name))
                bind_launch(db, key, lane, opened)
                db.execute(
                    "UPDATE deliveries SET thread=? WHERE id=?", (thread, identity)
                )
            reply = await deliver_to(db, codex, identity, new_thread=True)
    else:
        reply = await deliver(db, command, identity, environment=environment)
    hold = None
    if reply.strip() != f"TJ IMPLEMENTED {identity}":
        hold = reply.strip() or "The implementer did not report a completed change."
    with db:
        db.execute("UPDATE items SET phase='check',hold=? WHERE id=?", (hold, name))


async def advance(
    db: sqlite3.Connection,
    name: str,
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
) -> None:
    while True:
        row = item(db, name)
        if row["hold"] or row["phase"] in {"ready", "assigned", "closed"}:
            return
        if row["phase"] == "checking":
            raise TjenticError(
                "Check interrupted. Inspect the worktree and use workflow retry-check."
            )
        root = verify_tree(db, row)
        if row["phase"] == "implement":
            await _implemented(db, row, root, command, environment=environment)
            continue
        if row["phase"] == "check":
            run_check(db, row)
            continue
        check = latest_check(db, name)
        assert check is not None
        reply = await deliver(db, command, check["delivery"], environment=environment)
        hold = None
        if check["exit_code"] == 0:
            phase = "ready"
        else:
            phase = "check"
            if reply.strip() != f"TJ RECHECK {check['delivery']}":
                hold = reply.strip() or "The agent requested developer input."
            else:
                prior = db.execute(
                    "SELECT exit_code,output FROM checks WHERE item=? AND attempt=?",
                    (name, check["attempt"] - 1),
                ).fetchone()
                if (
                    prior
                    and prior["exit_code"] == check["exit_code"]
                    and prior["output"] == check["output"]
                ):
                    hold = "The repair repeated the same failure."
                elif check["attempt"] >= 4:
                    hold = "Automatic repair limit reached."
        with db:
            db.execute(
                "UPDATE items SET phase=?, hold=? WHERE id=?", (phase, hold, name)
            )


def status(db: sqlite3.Connection, name: str) -> dict[str, object]:
    row = item(db, name)
    check = latest_check(db, name)
    result = dict(row)
    result["command"] = json.loads(row["command"])
    result["last_check"] = dict(check) if check else None
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='pr_waits'").fetchone():
        wait = db.execute("SELECT * FROM pr_waits WHERE item=?", (name,)).fetchone()
        if wait:
            result["pr_wait"] = dict(wait)
            result["pr_decisions"] = [
                dict(entry)
                for entry in db.execute(
                    "SELECT id,item,pr,revision,merge_oid,host,profile,thread,"
                    "generation,kind,reason,delivery FROM pr_decisions "
                    "WHERE item=? ORDER BY id",
                    (name,),
                )
            ]
    intake = db.execute(
        "SELECT i.task,d.state FROM intakes i JOIN deliveries d ON d.id=i.delivery "
        "WHERE i.name=?",
        (name,),
    ).fetchone()
    if intake:
        result["implementation"] = dict(intake)
    selection = db.execute(
        "SELECT lane,model,effort,reason FROM lane_decisions "
        "WHERE thread=? ORDER BY rowid DESC LIMIT 1",
        (row["thread"],),
    ).fetchone()
    if selection:
        result["lane"] = dict(selection)
    last_transfer = db.execute(
        "SELECT * FROM transfers WHERE item=? ORDER BY rowid DESC LIMIT 1", (name,)
    ).fetchone()
    result["last_transfer"] = (
        {**dict(last_transfer), "item": row["name"]} if last_transfer else None
    )
    if last_transfer and last_transfer["action_delivery"]:
        action = db.execute(
            "SELECT state,reply FROM deliveries WHERE id=?",
            (last_transfer["action_delivery"],),
        ).fetchone()
        result["assignment"] = dict(action)
    if check:
        delivery = db.execute(
            "SELECT state FROM deliveries WHERE id=?", (check["delivery"],)
        ).fetchone()
        result["delivery_state"] = delivery["state"]
    return result


async def workflow(
    args: argparse.Namespace,
    command: Sequence[str],
    state: Path,
    config: Path,
    *,
    environment: Mapping[str, str],
) -> int:
    root = worktree(Path.cwd())
    with database(state) as db:
        if args.operation == "status":
            existing = find_item(db, root, args.item)
            if existing is None:
                raise TjenticError(
                    f"Unknown workflow item {args.item!r} in this worktree."
                )
            print(json.dumps(status(db, existing["id"])))
            return 0
        with claim_item(state, root, args.item):
            if args.operation == "start":
                row = await bind(
                    db,
                    args.item,
                    args.thread,
                    args.check,
                    command,
                    environment=environment,
                )
            elif args.operation == "run":
                row = await _begin(db, args, command, config, environment=environment)
            else:
                existing = find_item(db, root, args.item)
                if existing is None:
                    raise TjenticError(
                        f"Unknown workflow item {args.item!r} in this worktree."
                    )
                row = existing
            key = row["id"]
            pending = db.execute(
                "SELECT id FROM transfers WHERE item=? AND state='offered'",
                (key,),
            ).fetchone()
            if pending:
                raise TjenticError(
                    f"Outstanding handoff {pending['id']}. Retry or cancel the offer."
                )
            if args.operation == "hold":
                with db:
                    db.execute("UPDATE items SET hold=? WHERE id=?", (args.reason, key))
            elif args.operation == "release":
                with db:
                    db.execute("UPDATE items SET hold=NULL WHERE id=?", (key,))
            elif args.operation == "retry-check":
                if row["phase"] != "checking":
                    raise TjenticError("Only an interrupted check can be retried.")
                with db:
                    db.execute("UPDATE items SET phase='check' WHERE id=?", (key,))
            active = db.execute(
                "SELECT t.* FROM transfers t JOIN deliveries d "
                "ON t.action_delivery=d.id "
                "WHERE t.item=? AND t.receiver=? AND t.state='accepted' "
                "AND d.state!='done'",
                (key, row["thread"]),
            ).fetchone()
            if active and not item(db, key)["hold"]:
                launch_assignment(db, state, active, command, environment=environment)
                print(json.dumps(status(db, key)))
                return 0
            try:
                await advance(db, key, command, environment=environment)
            except (TjenticError, OSError, ValueError) as error:
                with db:
                    db.execute(
                        "UPDATE items SET last_error=? WHERE id=?",
                        (str(error), key),
                    )
                raise TjenticError(str(error)) from error
            with db:
                db.execute("UPDATE items SET last_error=NULL WHERE id=?", (key,))
            result = status(db, key)
            print(json.dumps(result))
            return 1 if result["hold"] else 0
