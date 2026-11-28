"""Read the workflow identity owned by the installed Rust listener."""

import json
import os
import sqlite3
import subprocess
import sysconfig
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from tjentic.errors import TjenticError


def listener_path() -> Path:
    name = "tj-listener.exe" if os.name == "nt" else "tj-listener"
    return Path(sysconfig.get_path("scripts")) / name


@dataclass(frozen=True, slots=True)
class Worktree:
    root: Path
    git_dir: Path
    device: str
    inode: str

    @classmethod
    def discover(cls, directory: Path) -> Self:
        result = subprocess.run(
            [listener_path(), "worktree", "--directory", directory],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if result.returncode:
            raise TjenticError(result.stderr.strip())
        record = json.loads(result.stdout)
        return cls(
            Path(record["root"]),
            Path(record["git_dir"]),
            record["device"],
            record["inode"],
        )

    def verify(self, row: sqlite3.Row, identity: sqlite3.Row | None) -> None:
        if self.root != Path(row["worktree"]) or self.git_dir != Path(row["git_dir"]):
            raise TjenticError(
                f"The registered Git worktree at {row['worktree']} changed."
            )
        if identity is not None and (self.device, self.inode) != (
            identity["device"],
            identity["inode"],
        ):
            raise TjenticError(
                f"The registered Git directory for {self.root} was replaced."
            )
