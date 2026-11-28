"""Check this checkout against its repository's locally recorded contributor policy."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from tjentic.errors import TjenticError
from tjentic.plugins.catalog import Catalog, ReadFile, read_file
from tjentic.sessions.hosts import Host

type Select = Callable[[Catalog, Path, ReadFile], list[str]]


@dataclass(frozen=True, slots=True)
class _Repository:
    root: Path
    environment: Mapping[str, str]

    @classmethod
    def discover(cls, directory: Path, environment: Mapping[str, str]) -> Self | None:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=directory,
            env={**environment, "LC_ALL": "C"},
            capture_output=True,
            check=False,
        )
        if not result.returncode:
            return cls(
                Path(os.fsdecode(result.stdout).rstrip("\r\n")).resolve(), environment
            )
        reason = os.fsdecode(result.stderr).strip()
        if reason.startswith("fatal: not a git repository (") or reason == (
            "fatal: this operation must be run in a work tree"
        ):
            return None
        raise TjenticError(f"Cannot identify the Git worktree in {directory}: {reason}")

    def git(self, *args: str, missing: bool = False) -> bytes:
        result = subprocess.run(
            ["git", "--no-replace-objects", "--literal-pathspecs", *args],
            cwd=self.root,
            # Reading a partial clone must never contact its promisor remote.
            env={
                **self.environment,
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_ALLOW_PROTOCOL": "",
            },
            capture_output=True,
            check=False,
        )
        if missing and result.returncode == 1:
            return b""
        if result.returncode:
            reason = os.fsdecode(result.stderr).strip()
            raise TjenticError(
                f"Cannot read Git contributor policy in {self.root}: {reason}"
            )
        return result.stdout

    def default(self) -> _Snapshot | None:
        output = self.git(
            "for-each-ref",
            "--format=%(symref) %(objectname)",
            "refs/remotes/*/HEAD",
        )
        refs = dict(
            line.split()
            for line in os.fsdecode(output).splitlines()
            if line.startswith("refs/remotes/")
        )
        ref = next(
            (name for name in refs if name.startswith("refs/remotes/origin/")), None
        )
        if ref is None and len(refs) == 1:
            ref = next(iter(refs))
        if ref:
            # The local default branch may contain an adoption commit not pushed yet.
            local = "refs/heads/" + ref.split("/", 3)[3]
            commit = self.git(
                "rev-parse",
                "--verify",
                "--quiet",
                f"{local}^{{commit}}",
                missing=True,
            ).strip()
            if commit and not self.git(
                "rev-list",
                "--max-count=1",
                refs[ref],
                "--not",
                commit.decode("ascii"),
            ):
                return _Snapshot(self, local, commit.decode("ascii"))
            return _Snapshot(self, ref, refs[ref])

        # This ref always denotes the primary worktree, even from a detached sibling.
        commit = self.git(
            "rev-parse",
            "--verify",
            "--quiet",
            "main-worktree/HEAD^{commit}",
            missing=True,
        )
        return (
            _Snapshot(self, "primary worktree HEAD", commit.decode("ascii").strip())
            if commit
            else None
        )


@dataclass(frozen=True, slots=True)
class _Snapshot:
    repo: _Repository
    branch: str
    commit: str

    def read(self, path: Path) -> bytes | None:
        relative = path.relative_to(self.repo.root).as_posix()
        record = self.repo.git("ls-tree", "-z", self.commit, "--", relative)
        if not record:
            return None
        mode, kind, oid = record.split(b"\t", 1)[0].split()
        if kind != b"blob" or mode not in (b"100644", b"100755"):
            raise TjenticError(f"Expected a tracked file at {self.branch}:{relative}.")
        return self.repo.git("cat-file", "blob", oid.decode("ascii"))


def checked_selection(
    directory: Path, host: Host, select: Select, environment: Mapping[str, str]
) -> tuple[Catalog | None, list[str]]:
    """Require this checkout to declare the plugins selected on its default branch."""
    repo = _Repository.discover(directory, environment)
    if repo is None:
        return None, []
    catalog = Catalog.read(repo.root)
    active = select(catalog, directory, read_file) if catalog else []
    default = repo.default()
    if default and (policy := Catalog.read(repo.root, default.read)):
        missing = sorted(
            set(select(policy, directory, default.read)).difference(active)
        )
        if missing:
            raise TjenticError(
                f"This checkout is missing {host} contributor plugins selected on "
                f"{default.branch}: {', '.join(missing)}. Update this branch or "
                "restore its tracked marketplace and host settings before continuing."
            )
    if catalog is None and (repo.root / ".tjentic" / "standards.toml").is_file():
        raise TjenticError(
            "This checkout selects Markdown files in .tjentic/standards.toml. "
            "Migrate to native SessionStart plugins before continuing."
        )
    return catalog, active
