import argparse
import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from importlib.metadata import version
from pathlib import Path
from uuid import UUID

from tjentic import standards
from tjentic.errors import TjenticError
from tjentic.sessions import claude
from tjentic.sessions.codex import Session, connect, terminal
from tjentic.sessions.hosts import Host
from tjentic.watch.native import Watch, listener_path
from tjentic.workspaces import Workspace


def _display(value: str) -> str:
    return "".join(character if character.isprintable() else " " for character in value)


def _product_file(parent: Path, name: str) -> Path:
    current = parent / "tjentic" / name
    previous = parent / "tjterm" / name
    if not current.exists() and previous.is_file():
        return previous
    return current


def _show(sessions: list[Session], json_output: bool) -> None:
    if json_output:
        print(
            json.dumps(
                [
                    {
                        "provider": "codex",
                        "id": str(session.id),
                        "cwd": str(session.directory),
                        "title": session.title,
                        "updated_at": int(session.updated_at.timestamp()),
                        "directory_exists": session.directory.is_dir(),
                    }
                    for session in sessions
                ]
            )
        )
        return
    if not sessions:
        print("No matching Codex sessions.")
        return
    for session in sessions:
        when = session.updated_at.strftime("%Y-%m-%d %H:%MZ")
        directory = _display(str(session.directory))
        missing = "" if session.directory.is_dir() else " [directory missing]"
        title = " ".join(_display(session.title).split())[:120] or "(untitled)"
        print(f"codex  {session.id}  {when}  {title}\n  {directory}{missing}")
    print("\nResume one with: tj recover --resume SESSION_ID")


async def _lookup(
    command: Sequence[str], args: argparse.Namespace, directory: Path
) -> list[Session]:
    async with connect(command) as codex:
        if args.resume:
            return [await codex.find(args.resume)]
        scope = None if args.all else Workspace.discover(directory)
        return await codex.search(args.query or "", scope, args.limit or 20)


def recover(args: argparse.Namespace, command: Sequence[str], directory: Path) -> int:
    sessions = asyncio.run(_lookup(command, args, directory))
    if not args.resume:
        _show(sessions, args.json)
        return 0
    session = sessions[0]
    if not session.directory.is_dir():
        raise TjenticError(f"Recorded directory is missing: {session.directory}")
    # An independent native client must acquire Codex's exclusive thread writer lock.
    result = subprocess.run(
        [
            *command,
            "resume",
            "--no-daemon",
            "--cd",
            str(session.directory),
            str(session.id),
        ],
        cwd=session.directory,
        check=False,
    )
    return result.returncode if result.returncode >= 0 else 128 - result.returncode


def main(
    argv: Sequence[str] | None = None, *, codex_command: Sequence[str] | None = None
) -> int:
    parser = argparse.ArgumentParser(prog="tj", description="TJentic")
    parser.add_argument(
        "--version", action="version", version=f"tj {version('tjentic')}"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    standards.add_command(commands)
    recovery = commands.add_parser(
        "recover", help="find a saved Codex conversation or resume its exact session"
    )
    recovery.add_argument("query", nargs="?", help="text from the conversation")
    recovery.add_argument("--all", action="store_true", help="search across projects")
    recovery.add_argument("--limit", type=int, help="maximum results (default 20)")
    recovery.add_argument(
        "--json", action="store_true", help="print structured search results"
    )
    recovery.add_argument(
        "--resume",
        type=UUID,
        metavar="SESSION_ID",
        help="resume in the recorded directory",
    )
    for name, description in (
        ("codex", "open a TJ-enabled Codex terminal"),
        ("claude", "open a native Claude Code terminal"),
    ):
        launcher = commands.add_parser(name, help=description)
        launcher.add_argument("--resume", type=UUID, metavar="SESSION_ID")
    watch_parser = commands.add_parser(
        "watch", help="receive actionable events for this native session"
    )
    watch_parser.add_argument(
        "--once", action="store_true", help="return after the first event"
    )
    transfer = commands.add_parser("handoff", help="give a task to a new agent tab")
    target = transfer.add_mutually_exclusive_group(required=True)
    target.add_argument("item", nargs="?", metavar="TASK")
    target.add_argument("--retry", type=UUID, metavar="TRANSFER_ID")
    target.add_argument("--cancel", type=UUID, metavar="TRANSFER_ID")
    target.add_argument("--accept", type=UUID, help=argparse.SUPPRESS)
    transfer.add_argument(
        "--to", type=UUID, metavar="THREAD", help="existing recipient in this worktree"
    )
    transfer.add_argument(
        "--next", metavar="ACTION", help="the recipient's next action"
    )
    transfer.add_argument(
        "--host",
        type=Host,
        choices=list(Host),
        help="provider when starting a handoff from the shell",
    )
    managed = commands.add_parser(
        "workflow", help="run an opt-in local check in a bound Codex session"
    )
    operations = managed.add_subparsers(dest="operation", required=True)
    start = operations.add_parser("start", help="bind an item and run its chosen check")
    start.add_argument("item")
    start.add_argument("--thread", required=True, type=UUID)
    run = operations.add_parser(
        "run", help="choose a lane and carry a task through checks"
    )
    run.add_argument("item")
    run.add_argument("--task", required=True, help="the authorized assignment")
    for entry in (start, run):
        entry.add_argument(
            "--check", required=True, nargs=argparse.REMAINDER, metavar="ARG"
        )
    for operation in ("resume", "status", "hold", "release", "retry-check"):
        action = operations.add_parser(operation)
        action.add_argument("item")
        if operation == "hold":
            action.add_argument("reason")
    transfer.add_argument(
        "--from",
        dest="sender",
        type=UUID,
        help="sender for an ad hoc item from the shell",
    )
    args = parser.parse_args(argv)
    if args.command == "workflow":
        if args.operation == "start" and not args.check:
            start.error("--check requires a command and its arguments")
        if args.operation == "run" and not args.check:
            run.error("--check requires a command and its arguments")
        if args.operation == "run" and not args.task.strip():
            run.error("--task requires an assignment")
    if args.command == "handoff":
        if args.item is not None and args.next is None:
            args.next = args.item
        if args.item is not None and (
            not args.item.strip() or not (args.next or "").strip()
        ):
            transfer.error("supply --next ACTION with the item")
        if (args.retry or args.cancel or args.accept) and (
            args.to or args.next or args.sender or args.host
        ):
            transfer.error("retry or cancel takes the saved transfer ID alone")
    if args.command == "recover":
        if args.limit is not None and args.limit < 1:
            recovery.error("--limit must be positive")
        if args.resume and (
            args.query is not None or args.all or args.json or args.limit is not None
        ):
            recovery.error(
                "--resume takes an exact session ID and cannot be combined with search"
            )
    try:
        if args.command == "plugins":
            standards.run_sync(
                Path.cwd(), apply=not args.dry_run, environment=os.environ
            )
            return 0
        if os.name == "nt":
            state = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local"
            config = os.environ.get("APPDATA") or Path.home() / "AppData/Roaming"
        else:
            state = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state"
            config = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
        workflow_state = _product_file(Path(state), "workflows.sqlite3").parent
        if args.command in {"codex", "claude", "watch"}:
            host = (
                Host.current(os.environ)
                if args.command == "watch"
                else Host(args.command)
            )
            workflow_state.mkdir(mode=0o700, parents=True, exist_ok=True)
            watch = Watch(
                host, workflow_state, host.profile(os.environ), listener_path()
            )
            watch.call("ensure")
            if args.command == "watch":
                try:
                    owner = UUID(os.environ.get(host.session_variable, ""))
                except ValueError as error:
                    raise TjenticError(
                        "Run tj watch inside a managed native session."
                    ) from error
                watch.call(
                    "watch",
                    "--host",
                    host,
                    "--profile",
                    str(watch.profile),
                    "--thread",
                    str(owner),
                    *(["--once"] if args.once else []),
                )
                return 0
            if host == Host.CLAUDE:
                return claude.launch(
                    host.command(), args.resume, os.environ, watch=watch
                )
            return asyncio.run(
                terminal(
                    codex_command or host.command(),
                    args.resume,
                    os.environ,
                    watch=watch,
                )
            )
        if args.command in {"workflow", "handoff"}:
            import sqlite3

            from tjentic.workflows.runner import workflow

            lanes = _product_file(Path(config), "lanes.toml")
            try:
                if args.command == "handoff":
                    from tjentic.terminal import Kitty
                    from tjentic.workflows.handoff import handoff

                    current = Host.current(os.environ)
                    caller = os.environ.get(current.session_variable)
                    host = args.host or current
                    if caller and host is not current:
                        raise TjenticError(
                            "Start the handoff using the sender's provider."
                        )
                    if caller and args.sender and str(args.sender) != caller:
                        raise TjenticError("The sender does not match this session.")
                    socket = os.environ.get("KITTY_LISTEN_ON")
                    window = os.environ.get("KITTY_WINDOW_ID")
                    terminal_target = (
                        Kitty(
                            socket,
                            window,
                            {
                                key: os.environ[key]
                                for key in (
                                    "PATH",
                                    "CODEX_HOME",
                                    "CLAUDE_CONFIG_DIR",
                                    "XDG_CONFIG_HOME",
                                    "XDG_STATE_HOME",
                                    "LOCALAPPDATA",
                                    "APPDATA",
                                )
                                if key in os.environ
                            },
                        )
                        if socket and window and window.isdecimal()
                        else None
                    )
                    return asyncio.run(
                        handoff(
                            args,
                            codex_command if host is Host.CODEX else None,
                            workflow_state,
                            caller or (str(args.sender) if args.sender else None),
                            lanes,
                            terminal_target,
                            os.environ.get("TJ_HANDOFF"),
                            host,
                            environment=os.environ,
                        )
                    )
                return asyncio.run(
                    workflow(
                        args,
                        []
                        if args.operation == "status"
                        else (codex_command or Host.CODEX.command()),
                        workflow_state,
                        lanes,
                    )
                )
            except sqlite3.Error as error:
                raise TjenticError(str(error)) from error
        return recover(args, codex_command or Host.CODEX.command(), Path.cwd())
    except (OSError, TjenticError) as error:
        print(f"tj: {_display(str(error))}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
