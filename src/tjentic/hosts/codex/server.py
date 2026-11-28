"""Communicate with the native app server without opening a model session."""

import json
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from threading import Timer
from typing import IO

from tjentic.hosts.host import Host
from tjentic.plugins.startup import StartupError, as_object


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
