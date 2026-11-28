import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from tjentic.errors import TjenticError


@dataclass(frozen=True, slots=True)
class Kitty:
    socket: str
    window: str
    environment: Mapping[str, str]

    def open_tab(
        self, directory: Path, title: str, command: Sequence[str], handoff: str
    ) -> None:
        overrides: list[str] = []
        for name, value in self.environment.items():
            overrides.extend(("--env", f"{name}={value}"))
        launch = subprocess.run(
            [
                "kitten",
                "@",
                "--to",
                self.socket,
                "launch",
                "--type",
                "tab",
                "--match",
                f"window_id:{self.window}",
                "--tab-title",
                title,
                "--cwd",
                str(directory),
                *overrides,
                "--env",
                f"TJ_HANDOFF={handoff}",
                "--env",
                "CODEX_THREAD_ID",
                "--hold",
                *command,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if launch.returncode or not launch.stdout.strip().isdecimal():
            raise TjenticError(
                launch.stderr.strip() or "Kitty could not open the recipient tab."
            )
