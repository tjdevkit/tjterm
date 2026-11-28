import asyncio
import json
import re
import subprocess
import tempfile
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from importlib.metadata import version
from pathlib import Path
from typing import Self
from uuid import UUID

from tjentic import standards
from tjentic.errors import TjenticError
from tjentic.hosts.host import Host
from tjentic.watch.native import Watch
from tjentic.workspaces import Workspace


class Model(StrEnum):
    ASTRA = "astra"
    SOL = "sol"
    TERRA = "terra"
    LUNA = "luna"

    def release_of(self, slug: str) -> tuple[int, ...] | None:
        match = re.fullmatch(rf"gpt-(\d+(?:\.\d+)*)-{self.value}", slug)
        if match is None:
            return None
        return tuple(int(part) for part in match[1].split("."))


@dataclass(frozen=True, slots=True)
class ModelSettings:
    model: Model | str
    effort: str


@dataclass(frozen=True)
class Session:
    id: UUID
    directory: Path
    title: str
    updated_at: datetime

    @classmethod
    def from_thread(cls, thread: object) -> Self:
        if not isinstance(thread, dict):
            raise TjenticError("Codex returned invalid thread metadata.")
        identity, cwd = thread.get("id"), thread.get("cwd")
        updated = thread.get("updatedAt")
        if not isinstance(identity, str) or not isinstance(cwd, str):
            raise TjenticError(
                "Codex returned a thread without an ID or working directory."
            )
        directory = Path(cwd)
        if "\x00" in cwd or not directory.is_absolute() or type(updated) is not int:
            raise TjenticError(
                "Codex returned an invalid working directory or timestamp."
            )
        try:
            session_id = UUID(identity)
        except ValueError as error:
            raise TjenticError("Codex returned an invalid session ID.") from error
        try:
            updated_at = datetime.fromtimestamp(updated, UTC)
        except (OSError, OverflowError, ValueError) as error:
            raise TjenticError(
                "Codex returned a timestamp outside the supported range."
            ) from error
        name = thread.get("name") or thread.get("preview")
        return cls(
            session_id, directory, name if isinstance(name, str) else "", updated_at
        )


@dataclass(frozen=True, slots=True)
class OpenedSession:
    session: Session
    settings: ModelSettings


@dataclass(frozen=True, slots=True)
class TerminalLaunch:
    arguments: tuple[str, ...]
    settings: ModelSettings


def _message(line: bytes) -> dict[str, object]:
    reply = json.loads(line)
    if not isinstance(reply, dict):
        raise TjenticError("Codex returned an invalid protocol message.")
    if "method" in reply and "id" in reply:
        raise TjenticError(f"Codex needs developer input for {reply['method']}.")
    return reply


def receipt(turns: list[object], prompt: str) -> str | None:
    for turn in reversed(turns):
        if not isinstance(turn, dict) or not isinstance(turn.get("items"), list):
            raise TjenticError("Cannot reconcile incomplete Codex history.")
        items = turn["items"]
        for entry in items:
            if not isinstance(entry, dict) or entry.get("type") != "userMessage":
                continue
            content = entry.get("content")
            if isinstance(content, list) and any(
                isinstance(part, dict)
                and part.get("type") == "text"
                and part.get("text") == prompt
                for part in content
            ):
                if turn.get("status") != "completed":
                    raise TjenticError(
                        f"Delivery found in a {turn.get('status')} Codex turn."
                    )
                for response in reversed(items):
                    if (
                        isinstance(response, dict)
                        and response.get("type") == "agentMessage"
                        and response.get("phase") != "commentary"
                        and isinstance(response.get("text"), str)
                    ):
                        return str(response["text"])
                raise TjenticError("Delivered turn has no final Codex reply.")
    return None


class Codex:
    """Query saved conversations through the local Codex app server."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        environment: Mapping[str, str],
    ):
        self.environment = environment
        self.reader = reader
        self.writer = writer
        self.request_id = 0
        self._catalog: list[str] | None = None

    async def _model(self, family: Model | str) -> str:
        if not isinstance(family, Model):
            return family
        if self._catalog is None:
            self._catalog = await self._read_catalog()
        selected: str | None = None
        version: tuple[int, ...] = ()
        for slug in self._catalog:
            candidate = family.release_of(slug)
            if candidate is not None and candidate > version:
                selected, version = slug, candidate
        if selected is None:
            raise TjenticError(f"Codex did not report a versioned {family} model.")
        return selected

    async def _read_catalog(self) -> list[str]:
        params: dict[str, object] = {"includeHidden": False}
        models: list[str] = []
        cursors: set[str] = set()
        while True:
            page = await self.request("model/list", params)
            entries = page.get("data")
            if not isinstance(entries, list):
                raise TjenticError("Codex returned an invalid model catalog.")
            models.extend(
                entry["model"]
                for entry in entries
                if isinstance(entry, dict) and isinstance(entry.get("model"), str)
            )
            cursor = page.get("nextCursor")
            if cursor is None:
                return models
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise TjenticError("Invalid Codex model catalog cursor.")
            cursors.add(cursor)
            params["cursor"] = cursor

    async def request(
        self, method: str, params: dict[str, object]
    ) -> dict[str, object]:
        self.request_id += 1
        message = {"id": self.request_id, "method": method, "params": params}
        self.writer.write(json.dumps(message).encode() + b"\n")
        try:
            async with asyncio.timeout(60):
                await self.writer.drain()
                while line := await self.reader.readline():
                    reply = _message(line)
                    if reply.get("id") != self.request_id or "method" in reply:
                        continue
                    error = reply.get("error")
                    if isinstance(error, dict):
                        raise TjenticError(
                            f"Codex {method}: {error.get('message', 'failed')}"
                        )
                    result = reply.get("result")
                    if not isinstance(result, dict):
                        raise TjenticError(
                            f"Codex returned an invalid result for {method}."
                        )
                    return result
        except TimeoutError as error:
            raise TjenticError(f"Codex timed out during {method}.") from error
        except (OSError, ValueError) as error:
            raise TjenticError(f"Could not read Codex {method}: {error}") from error
        raise TjenticError(f"Codex exited before answering {method}.")

    async def search(
        self, query: str, workspace: Workspace | None, limit: int
    ) -> list[Session]:
        method = "thread/search" if query else "thread/list"
        params: dict[str, object] = {
            "currentAccountOnly": True,
            "limit": 50,
            "sortKey": "updated_at",
            "sortDirection": "desc",
        }
        if query:
            params["searchTerm"] = query
        else:
            params["modelProviders"] = []
        sessions: list[Session] = []
        cursors: set[str] = set()
        seen: set[UUID] = set()
        while True:
            result = await self.request(method, params)
            rows = result.get("data")
            if not isinstance(rows, list):
                raise TjenticError(f"Codex returned an invalid page for {method}.")
            for row in rows:
                thread = row.get("thread") if query and isinstance(row, dict) else row
                session = Session.from_thread(thread)
                if session.id in seen:
                    continue
                seen.add(session.id)
                if workspace is None or workspace.contains(session.directory):
                    sessions.append(session)
                    if len(sessions) == limit:
                        return sessions
            cursor = result.get("nextCursor")
            if cursor is None:
                return sessions
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise TjenticError(
                    "Codex returned an invalid or repeated history cursor."
                )
            cursors.add(cursor)
            params["cursor"] = cursor

    async def find(self, session_id: UUID, *, workflow: bool = False) -> Session:
        params: dict[str, object] = {
            "currentAccountOnly": True,
            "modelProviders": [],
            "includedThreadIds": [str(session_id)],
            "limit": 1,
        }
        if workflow:
            params["sourceKinds"] = ["cli", "exec", "vscode", "appServer"]
        result = await self.request("thread/list", params)
        rows = result.get("data")
        if not isinstance(rows, list) or len(rows) != 1:
            raise TjenticError(
                f"No unique active-history Codex session matches {session_id}."
            )
        session = Session.from_thread(rows[0])
        if session.id != session_id:
            raise TjenticError(
                "Codex returned a different session for the requested ID."
            )
        return session

    async def create(
        self, directory: Path, model: Model | str, *, effort: str
    ) -> OpenedSession:
        await asyncio.to_thread(
            standards.verify, Host.CODEX, directory, self.environment
        )
        settings = ModelSettings(await self._model(model), effort)
        result = await self.request(
            "thread/start",
            {
                "cwd": str(directory),
                "model": settings.model,
                "config": {"model_reasoning_effort": settings.effort},
                "allowProviderModelFallback": False,
            },
        )
        if (result.get("model"), result.get("reasoningEffort")) != (
            settings.model,
            settings.effort,
        ):
            raise TjenticError("Codex did not apply the requested model and effort.")
        session = Session.from_thread(result.get("thread"))
        if session.directory.resolve() != directory:
            raise TjenticError(
                "Codex created a session outside the requested worktree."
            )
        return OpenedSession(session, settings)

    async def name(self, identity: UUID, title: str) -> None:
        await self.request(
            "thread/name/set", {"threadId": str(identity), "name": title}
        )

    async def read(self, identity: UUID) -> dict[str, object]:
        data = await self.request("thread/read", {"threadId": str(identity)})
        thread = data.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != str(identity):
            raise TjenticError("Codex did not return the requested thread.")
        return thread

    async def transcript(self, identity: UUID) -> Path:
        thread = await self.read(identity)
        recorded = thread.get("path")
        if isinstance(recorded, str) and Path(recorded).is_absolute():
            return Path(recorded)
        raise TjenticError("Codex did not report the recipient's local transcript.")

    async def terminal_launch(
        self,
        directory: Path,
        model: Model | str,
        effort: str,
        prompt: str | None,
        recipient: UUID | None = None,
    ) -> TerminalLaunch:
        model_id = await self._model(model)
        args: tuple[str, ...] = (
            "--cd",
            str(directory),
            "--model",
            model_id,
            "-c",
            f"model_reasoning_effort={json.dumps(effort)}",
        )
        if recipient is not None:
            args = ("resume", *args, str(recipient))
        if prompt is not None:
            args = (*args, "--", prompt)
        return TerminalLaunch(args, ModelSettings(model_id, effort))

    async def assess(
        self,
        command: Sequence[str],
        directory: Path,
        prompt: str,
        schema: object,
        model: Model | str,
        *,
        effort: str,
    ) -> str:
        await asyncio.to_thread(
            standards.verify, Host.CODEX, directory, self.environment
        )
        model = await self._model(model)
        with tempfile.TemporaryDirectory(prefix="tj-assessment-") as temp:
            path = Path(temp)
            schema_file = path / "schema.json"
            result = path / "result.json"
            errors = path / "stderr"
            schema_file.write_text(json.dumps(schema), encoding="utf-8")
            with errors.open("wb") as stderr:
                proc = await asyncio.create_subprocess_exec(
                    *command,
                    "exec",
                    "--ephemeral",
                    "--sandbox",
                    "read-only",
                    "--model",
                    model,
                    "-c",
                    'approval_policy="never"',
                    "-c",
                    f"model_reasoning_effort={json.dumps(effort)}",
                    "--output-schema",
                    str(schema_file),
                    "--output-last-message",
                    str(result),
                    "-",
                    cwd=directory,
                    env=self.environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr,
                )
                try:
                    async with asyncio.timeout(180):
                        await proc.communicate(prompt.encode())
                except TimeoutError as error:
                    raise TjenticError("Codex lane assessment timed out.") from error
                finally:
                    if proc.returncode is None:
                        proc.kill()
                        await proc.wait()
            if proc.returncode or not result.is_file():
                with errors.open("rb") as stderr:
                    stderr.seek(max(0, errors.stat().st_size - 1000))
                    detail = stderr.read().decode(errors="replace").strip()
                raise TjenticError(f"Codex lane assessment failed. {detail}")
            return result.read_text(encoding="utf-8")

    async def acquire(self, identity: UUID, directory: Path) -> None:
        """Acquire the native writer before inspecting history or submitting a turn."""
        session = await self.find(identity, workflow=True)
        if session.directory.resolve() != directory or not directory.is_dir():
            raise TjenticError(
                f"Codex session {identity} has a different or missing directory."
            )
        await asyncio.to_thread(
            standards.verify, Host.CODEX, directory, self.environment
        )
        result = await self.request(
            "thread/resume",
            {"threadId": str(identity), "cwd": str(directory), "excludeTurns": True},
        )
        thread = result.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != str(identity):
            raise TjenticError("Codex resumed a different thread.")
        if Path(str(result.get("cwd"))).resolve() != directory:
            raise TjenticError("The resumed Codex working directory has changed.")

    async def send(
        self,
        identity: UUID,
        delivery: str,
        prompt: str,
        settings: ModelSettings | None = None,
    ) -> None:
        params: dict[str, object] = {
            "threadId": str(identity),
            "clientUserMessageId": delivery,
            "input": [{"type": "text", "text": prompt}],
        }
        if settings is not None:
            params.update(model=settings.model, effort=settings.effort)
        reply = await self.request("turn/start", params)
        turn = reply.get("turn")
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
            raise TjenticError("Missing Codex turn ID.")
        while line := await self.reader.readline():
            msg = _message(line)
            if msg.get("method") == "turn/completed":
                event = msg.get("params")
                if isinstance(event, dict) and event.get("threadId") == str(identity):
                    completed = event.get("turn")
                    if (
                        isinstance(completed, dict)
                        and completed.get("id") == turn["id"]
                    ):
                        return
        raise TjenticError("Codex closed during the assignment.")

    async def received(self, identity: UUID, prompt: str) -> str | None:
        params: dict[str, object] = {
            "threadId": str(identity),
            "itemsView": "full",
            "limit": 8,
            "sortDirection": "desc",
        }
        cursors: set[str] = set()
        while True:
            page = await self.request("thread/turns/list", params)
            turns = page.get("data")
            if not isinstance(turns, list):
                raise TjenticError("Missing Codex turns.")
            reply = receipt(turns, prompt)
            if reply is not None:
                return reply
            cursor = page.get("nextCursor")
            if cursor is None:
                return None
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise TjenticError("Invalid Codex turn history cursor.")
            params["cursor"] = cursor
            cursors.add(cursor)


@asynccontextmanager
async def connect(
    command: Sequence[str],
    *,
    directory: Path | None = None,
    environment: Mapping[str, str],
) -> AsyncIterator[Codex]:
    process = await asyncio.create_subprocess_exec(
        *command,
        "app-server",
        env=environment,
        cwd=directory,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        limit=32 * 1024 * 1024,
    )
    assert process.stdout is not None and process.stdin is not None
    client = Codex(process.stdout, process.stdin, environment)
    try:
        await client.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "tjentic",
                    "title": "TJentic",
                    "version": version("tjentic"),
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        process.stdin.write(b'{"method":"initialized"}\n')
        yield client
    finally:
        process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            if process.returncode is None:
                process.kill()
            await process.wait()


_HANDOFF_HINT = (
    "This TJ-enabled terminal supports task handoff. When the user asks to hand off "
    "a new task, run tj handoff 'TASK'. For an existing tracked item run "
    "tj handoff ITEM --next 'ACTION'. An accepted "
    "receipt transfers only ITEM. Continue the user's other work here. For an "
    "interrupted offer use tj handoff --retry TRANSFER_ID."
)


async def terminal(
    command: Sequence[str],
    identity: UUID | None,
    environment: Mapping[str, str],
    *,
    watch: Watch | None = None,
) -> int:
    directory = Path.cwd()
    async with connect(command, environment=environment) as codex:
        if identity is not None:
            directory = (await codex.find(identity, workflow=True)).directory
            # Codex can replay the original startup instructions on exact resume.
            # Place the capability in the resumed context without starting a model turn.
            try:
                await codex.acquire(identity, directory.resolve())
            except TjenticError as error:
                # A managed TUI can attach to its already-loaded daemon thread.
                # A second app-server process cannot acquire that thread's writer.
                if watch is None or "already has an active writer" not in str(error):
                    raise
            else:
                await codex.request(
                    "thread/inject_items",
                    {
                        "threadId": str(identity),
                        "items": [
                            {
                                "type": "message",
                                "role": "developer",
                                "content": [
                                    {"type": "input_text", "text": _HANDOFF_HINT}
                                ],
                            }
                        ],
                    },
                )
            args = ["resume"]
        else:
            await asyncio.to_thread(
                standards.verify, Host.CODEX, directory, environment
            )
            response = await codex.request("config/read", {"cwd": str(directory)})
            config = response.get("config")
            if not isinstance(config, dict):
                raise TjenticError("Missing Codex configuration.")
            existing = config.get("developer_instructions") or ""
            if not isinstance(existing, str):
                raise TjenticError("Invalid Codex developer instructions.")
            instructions = (existing + "\n\n" + _HANDOFF_HINT).strip()
            args = [
                "-c",
                "developer_instructions="
                + json.dumps(instructions, ensure_ascii=False),
            ]
    if watch is None:
        args.append("--no-daemon")
    args.extend(["--cd", str(directory)])
    if identity is not None:
        args.append(str(identity))
    if watch is not None:
        args = [*watch.codex_config(directory), *args]
    process = await asyncio.create_subprocess_exec(
        *command, *args, cwd=directory, env={**environment, "TJ_HOST": Host.CODEX}
    )
    result = await process.wait()
    return result if result >= 0 else 128 - result
