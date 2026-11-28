import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from tjentic.errors import TjenticError
from tjentic.hosts.host import Host


@dataclass(frozen=True, slots=True)
class Watch:
    host: Host
    state: Path
    profile: Path
    listener: Path

    def call(self, *args: str, capture: bool = False) -> str:
        result = subprocess.run(
            [self.listener, "--state", self.state, "--python", sys.executable, *args],
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if result.returncode:
            raise TjenticError(result.stderr.strip() or "TJ listener exited.")
        return result.stdout or ""

    def tool_args(self, directory: Path, session: UUID | None = None) -> list[str]:
        args = [
            "-m",
            "tjentic.watch.tools",
            "--host",
            self.host.value,
            "--state",
            str(self.state),
            "--profile",
            str(self.profile),
            "--directory",
            str(directory),
            "--listener",
            str(self.listener),
        ]
        if session is not None:
            args.extend(["--session", str(session)])
        return args

    def codex_config(self, directory: Path) -> list[str]:
        return [
            "-c",
            "mcp_servers.tj_watch.command=" + json.dumps(sys.executable),
            "-c",
            'mcp_servers.tj_watch.env_vars=["PATH","GH_CONFIG_DIR",'
            '"GH_TOKEN","GITHUB_TOKEN","XDG_CONFIG_HOME"]',
            "-c",
            "mcp_servers.tj_watch.args=" + json.dumps(self.tool_args(directory)),
        ]

    def claude_config(self, directory: Path, session: UUID) -> list[str]:
        server = {"command": sys.executable, "args": self.tool_args(directory, session)}
        return [
            "--mcp-config",
            json.dumps({"mcpServers": {"tj_watch": server}}),
            "--plugin-dir",
            str(Path(__file__).with_name("plugin")),
        ]

    def register(self, session: UUID, directory: Path, item: str, issue: str) -> str:
        return self.call(
            "register",
            "--host",
            self.host,
            "--thread",
            str(session),
            "--profile",
            str(self.profile),
            "--directory",
            str(directory),
            "--item",
            item,
            "--issue",
            issue,
            capture=True,
        )
