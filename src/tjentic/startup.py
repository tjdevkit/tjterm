"""Inspect the native host's startup registration without running its hooks."""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Timer
from typing import IO

from tjentic.errors import TjenticError
from tjentic.sessions.hosts import Host


class StartupError(TjenticError):
    """The native host cannot verify a selected standards registration."""


def as_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise StartupError("Expected a JSON object from the native host.")
    return value


def as_rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise StartupError("Expected a JSON list from the native host.")
    return [as_object(row) for row in value]


def read_object(path: Path) -> dict[str, object]:
    try:
        return as_object(json.loads(path.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError) as error:
        raise StartupError(f"Cannot read {path}: {error}") from error


class CodexServer:
    def __init__(self, writer: IO[str], reader: IO[str]) -> None:
        self.writer = writer
        self.reader = reader
        self.sequence = 0

    def send(self, message: Mapping[str, object]) -> None:
        self.writer.write(json.dumps(message) + "\n")
        self.writer.flush()

    def request(self, method: str, params: Mapping[str, object]) -> dict[str, object]:
        self.sequence += 1
        self.send({"id": self.sequence, "method": method, "params": params})
        for line in self.reader:
            response = as_object(json.loads(line))
            if response.get("id") == self.sequence:
                if "error" in response:
                    raise StartupError(f"Codex rejected {method}: {response['error']}")
                return as_object(response.get("result"))
        raise StartupError(f"Codex closed before answering {method}.")


@contextmanager
def codex_server(
    directory: Path, environment: Mapping[str, str]
) -> Iterator[CodexServer]:
    """Connect to the app-server in this checkout, without starting a model session."""
    try:
        with subprocess.Popen(
            [*Host.CODEX.command(), "app-server"],
            cwd=directory,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
        ) as server:
            assert server.stdin is not None and server.stdout is not None
            client = CodexServer(server.stdin, server.stdout)
            timeout = Timer(60, server.kill)
            timeout.start()
            try:
                client.request(
                    "initialize",
                    {
                        "clientInfo": {"name": "tj", "version": "1"},
                        "capabilities": {"experimentalApi": True},
                    },
                )
                client.send({"method": "initialized"})
                yield client
            finally:
                timeout.cancel()
                server.kill()
    except (OSError, ValueError) as error:
        raise StartupError(f"Could not communicate with Codex: {error}") from error


@dataclass(frozen=True, slots=True)
class Installation:
    root: Path
    revision: str

    def startup_commands(self) -> list[str]:
        manifest = self.root / "hooks" / "hooks.json"
        groups = as_rows(
            as_object(read_object(manifest).get("hooks")).get("SessionStart")
        )
        commands: list[str] = []
        for group in groups:
            if group.get("matcher") not in (None, "", "*"):
                raise StartupError(
                    f"Conditional SessionStart registration in {manifest}."
                )
            for handler in as_rows(group.get("hooks")):
                command = handler.get("command")
                if (
                    handler.get("type") != "command"
                    or handler.get("async", False)
                    or not isinstance(command, str)
                    or not command.strip()
                ):
                    raise StartupError(
                        f"Expected a synchronous SessionStart command in {manifest}."
                    )
                commands.append(command)
        if not commands:
            raise StartupError(f"No SessionStart commands in {manifest}.")
        return commands

    def check_commands(
        self, identity: str, actual: Iterable[str], *, expand_root: bool = False
    ) -> None:
        expected = self.startup_commands()
        if expand_root:
            expected = [
                command.replace("${CLAUDE_PLUGIN_ROOT}", str(self.root)).replace(
                    "${PLUGIN_ROOT}", str(self.root)
                )
                for command in expected
            ]
        if Counter(expected) != Counter(actual):
            raise StartupError(
                f"Incomplete native SessionStart registration for {identity}."
            )

    def check_alias(
        self, identity: str, other: str, installation: Installation | None
    ) -> None:
        if installation is not None and installation.revision == self.revision:
            raise StartupError(
                f"{identity} and {other} both register the same standards revision. "
                f"Disable the duplicate plugin {other} in this checkout."
            )


def require_revision(
    identity: str, install: Installation | None, pin: str
) -> Installation:
    if install is None or install.revision != pin:
        actual = install.revision if install else "missing"
        raise StartupError(
            f"{identity} needs {pin}, found {actual}. Run tj plugins update here."
        )
    return install
