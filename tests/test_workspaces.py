import subprocess
from pathlib import Path

import pytest

from tjentic.workspaces import Workspace


def test_nested_repository_keeps_its_own_sessions(tmp_path: Path) -> None:
    inner = tmp_path / "nested"
    inner.mkdir()
    (inner / ".git").mkdir()
    workspace = Workspace(tmp_path, frozenset({tmp_path}))
    assert workspace.contains(tmp_path / "component")
    assert not workspace.contains(inner / "component")


def test_plain_directory_matches_its_own_sessions(tmp_path: Path) -> None:
    workspace = Workspace.discover(tmp_path)
    assert workspace.contains(tmp_path)
    assert not workspace.contains(tmp_path.parent)


def test_registered_paths_belong_to_the_project_that_currently_uses_them(
    tmp_path: Path,
) -> None:
    def git(*args: str | Path) -> None:
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            check=True,
            capture_output=True,
        )

    project = tmp_path / "project"
    bare = tmp_path / "project.git"
    current = tmp_path / "current"
    retired = tmp_path / "retired"
    offline = tmp_path / "offline"
    git("init", project)
    git("-C", project, "commit", "--allow-empty", "-m", "Initial")
    git("clone", "--bare", project, bare)
    for checkout in (current, retired, offline):
        git("-C", bare, "worktree", "add", "--detach", checkout)
    retired.rename(tmp_path / "moved")
    git("init", retired)
    offline.rename(tmp_path / "unmounted")

    workspace = Workspace.discover(current)
    assert workspace.contains(current / "src")
    assert workspace.contains(offline / "src")
    assert not workspace.contains(retired / "src")
    assert not workspace.contains(bare / "objects")


def test_unresolvable_session_path_does_not_break_project_filtering(
    tmp_path: Path,
) -> None:
    loop = tmp_path / "loop"
    try:
        loop.symlink_to(loop, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Directory symlinks are unavailable: {error}")
    workspace = Workspace(tmp_path, frozenset())
    assert not workspace.contains(loop / "old-session")
    assert workspace.contains(tmp_path)
