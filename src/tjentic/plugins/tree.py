"""Verify a native plugin copied from a Git marketplace subdirectory."""

import hashlib
import os
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from tjentic.errors import TjenticError


class TreeError(TjenticError):
    """The installed package does not match its publisher's committed tree."""


@dataclass(frozen=True, slots=True)
class _Entry:
    mode: bytes
    oid: bytes

    def matches(self, path: Path) -> bool:
        metadata = path.lstat()
        mode = metadata.st_mode
        if self.mode == b"040000":
            return stat.S_ISDIR(mode)
        digest = hashlib.new(
            "sha1" if len(self.oid) == 40 else "sha256", usedforsecurity=False
        )
        if self.mode == b"120000" and stat.S_ISLNK(mode):
            body = os.fsencode(path.readlink())
            digest.update(f"blob {len(body)}\0".encode("ascii"))
            digest.update(body)
        elif self.mode in (b"100644", b"100755") and stat.S_ISREG(mode):
            if os.name != "nt" and bool(mode & stat.S_IXUSR) != (
                self.mode == b"100755"
            ):
                return False
            # Hash the literal Git blob. Never run worktree filters.
            digest.update(f"blob {metadata.st_size}\0".encode("ascii"))
            with path.open("rb") as stream:
                while chunk := stream.read(262144):
                    digest.update(chunk)
        else:
            return False
        return digest.hexdigest().encode("ascii") == self.oid


@dataclass(frozen=True, slots=True)
class PackageTree:
    revision: str
    entries: dict[str, _Entry]

    @classmethod
    def read(
        cls, checkout: Path, revision: str, subdir: str, environment: Mapping[str, str]
    ) -> "PackageTree":
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.longpaths=true",
                "--no-replace-objects",
                "ls-tree",
                "-rtz",
                f"{revision}:{subdir}",
            ],
            cwd=checkout,
            env={**environment, "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": ""},
            capture_output=True,
            check=False,
        )
        if result.returncode or not result.stdout:
            raise TreeError(
                f"Missing committed plugin directory {subdir} at {revision}."
            )
        entries = {}
        for row in result.stdout.rstrip(b"\0").split(b"\0"):
            metadata, path = row.split(b"\t", 1)
            mode, _, oid = metadata.split()
            entries[os.fsdecode(path)] = _Entry(mode, oid)
        return cls(revision, entries)

    def verify(
        self, root: Path, *, native_metadata: Callable[[Path, Path], bool] | None = None
    ) -> None:
        seen = set()
        try:
            for parent, directories, files in root.walk():
                for name in [*directories, *files]:
                    path = parent / name
                    key = path.relative_to(root).as_posix()
                    expected = self.entries.get(key)
                    if (
                        expected is None
                        and native_metadata
                        and native_metadata(root, path)
                    ):
                        continue
                    if expected is None or not expected.matches(path):
                        raise TreeError(f"{path} differs from the selected Git tree.")
                    seen.add(key)
        except OSError as error:
            raise TreeError(f"Cannot verify the installed plugin at {root}.") from error
        if seen != self.entries.keys():
            raise TreeError(
                f"Incomplete installed plugin at {root}. Run tj plugins update here."
            )

    def installed_root(self, identity: str, paths: Sequence[object]) -> Path:
        candidates: set[Path] | None = None
        for value in paths:
            if not isinstance(value, str) or not (path := Path(value)).is_absolute():
                raise TreeError(f"Invalid native resource location for {identity}.")
            matching = {
                parent
                for parent in path.parents
                if path.relative_to(parent).as_posix() in self.entries
            }
            candidates = matching if candidates is None else candidates & matching
        if not candidates or len(candidates) != 1:
            raise TreeError(f"Cannot locate the installed Git subtree of {identity}.")
        root = candidates.pop()
        self.verify(root)
        return root
