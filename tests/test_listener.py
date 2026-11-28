import subprocess
from pathlib import Path

from tjentic.listener import Worktree


def test_linked_worktree_matches_existing_python_bindings(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    subprocess.run(["git", "init", "-q", primary], check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.org",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "-qm",
            "first",
        ],
        cwd=primary,
        check=True,
    )
    linked = tmp_path / "linked café"
    subprocess.run(
        ["git", "worktree", "add", "--detach", linked], cwd=primary, check=True
    )
    nested = linked / "component"
    nested.mkdir()
    tree = Worktree.discover(nested)
    git_dir = Path(
        subprocess.check_output(
            ["git", "rev-parse", "--absolute-git-dir"],
            cwd=linked,
            text=True,
            encoding="utf-8",
        ).rstrip("\n")
    )
    previous = git_dir.stat()
    assert tree == Worktree(
        linked.resolve(), git_dir, str(previous.st_dev), str(previous.st_ino)
    )
    assert tree.git_dir != Worktree.discover(primary).git_dir
