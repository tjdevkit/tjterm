import os
import shutil
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Self

from tjentic.errors import TjenticError


class Host(StrEnum):
    CODEX = ("codex", "Codex", "CODEX_THREAD_ID", "CODEX_HOME")
    CLAUDE = ("claude", "Claude Code", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CONFIG_DIR")

    session_variable: str
    profile_variable: str
    provider: str

    def __new__(cls, value: str, *metadata: str) -> Self:
        member = str.__new__(cls, value)
        member._value_ = value
        member.provider, member.session_variable, member.profile_variable = metadata
        return member

    def profile(self, environment: Mapping[str, str]) -> Path:
        return (
            Path(environment.get(self.profile_variable) or Path.home() / f".{self}")
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
            raise TjenticError(
                f"Install the native {self.provider} CLI and make it available on PATH."
            )
        return [str(Path(executable).resolve())]
