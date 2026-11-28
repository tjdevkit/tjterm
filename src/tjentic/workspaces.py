import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from tjentic.errors import TjenticError


def _git_output(directory: Path, *args: str) -> bytes | None:
    result = subprocess.run(
        ["git", "-C", str(directory), *args],
        capture_output=True,
        check=False,
        timeout=10,
    )
    return result.stdout if result.returncode == 0 else None


def _resolve_recorded_path(path: Path) -> Path | None:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        # An old path can contain a symlink loop, including on Python 3.12.
        return None


def _registered_roots(directory: Path, listing: bytes) -> frozenset[Path]:
    identity = ("rev-parse", "--path-format=absolute", "--git-common-dir")
    common_dir = _git_output(directory, *identity)
    if common_dir is None:
        raise TjenticError("Git could not identify this project's repository.")
    roots: set[Path] = set()
    for entry in listing.split(b"\0\0"):
        location, *attributes = entry.split(b"\0")
        if not location.startswith(b"worktree ") or b"bare" in attributes:
            continue
        root = _resolve_recorded_path(
            Path(os.fsdecode(location.removeprefix(b"worktree ")))
        )
        if root is None:
            continue
        # Git retains missing worktrees, even when another repo takes their old path.
        if (
            root != directory
            and root.exists()
            and (
                not (root / ".git").exists()
                or _git_output(root, *identity) != common_dir
            )
        ):
            continue
        roots.add(root)
    return frozenset(roots)


@dataclass(frozen=True)
class Workspace:
    directory: Path
    worktrees: frozenset[Path]

    @classmethod
    def discover(cls, directory: Path) -> Self:
        directory = directory.resolve()
        try:
            listing = _git_output(directory, "worktree", "list", "--porcelain", "-z")
            if listing is not None:
                return cls(directory, _registered_roots(directory, listing))
        except FileNotFoundError:
            return cls(directory, frozenset())
        except (OSError, subprocess.TimeoutExpired) as error:
            raise TjenticError(f"Could not discover Git worktrees: {error}") from error
        if any((path / ".git").exists() for path in (directory, *directory.parents)):
            raise TjenticError("Git could not list this project's worktrees.")
        return cls(directory, frozenset())

    def contains(self, directory: Path) -> bool:
        resolved = _resolve_recorded_path(directory)
        if resolved is None:
            return False
        if not self.worktrees:
            return resolved == self.directory
        for path in (resolved, *resolved.parents):
            if path in self.worktrees:
                return True
            if (path / ".git").exists():
                return False
        return False
