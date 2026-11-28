import os
import shutil
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Self

from tjentic.errors import TjenticError


class Host(StrEnum):
    CODEX = "codex"
    CLAUDE = "claude"

    @property
    def session_variable(self) -> str:
        return "CODEX_THREAD_ID" if self is Host.CODEX else "CLAUDE_CODE_SESSION_ID"

    def profile(self, environment: Mapping[str, str]) -> Path:
        key = "CLAUDE_CONFIG_DIR" if self is Host.CLAUDE else "CODEX_HOME"
        return (
            Path(environment.get(key) or Path.home() / f".{self}")
            .expanduser()
            .resolve()
        )

    @classmethod
    def current(cls, environment: Mapping[str, str]) -> Self:
        if environment.get("TJ_HOST") == cls.CLAUDE:
            return cls.CLAUDE
        if environment.get(cls.CODEX.session_variable):
            return cls.CODEX
        if environment.get(cls.CLAUDE.session_variable) or environment.get(
            "CLAUDECODE"
        ):
            return cls.CLAUDE
        return cls.CODEX

    def command(self) -> list[str]:
        executable = shutil.which(
            f"{self.value}.exe" if os.name == "nt" else self.value
        )
        if executable is None:
            provider = "Claude Code" if self is Host.CLAUDE else "Codex"
            raise TjenticError(
                f"Install the native {provider} CLI and make it available on PATH."
            )
        return [str(Path(executable).resolve())]
