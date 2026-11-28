import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tjentic.cli import main
from tjentic.errors import TjenticError
from tjentic.hosts.host import Host
from tjentic.standards import verify


def _git(directory: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=directory,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


@pytest.fixture
def adopted(tmp_path: Path) -> Path:
    primary = tmp_path / "project"
    primary.mkdir()
    _git(primary, "init", "-q", "--initial-branch=trunk")
    _git(primary, "commit", "--allow-empty", "-qm", "Before adoption")
    _git(primary, "tag", "before-adoption")
    project = Path(__file__).resolve().parents[1]
    for name in (".claude-plugin", ".claude", ".codex"):
        shutil.copytree(project / name, primary / name)
    _git(primary, "add", ".")
    _git(primary, "commit", "-qm", "Adopt the contributor standards")
    return primary


@pytest.mark.parametrize("nested", [True, False])
@pytest.mark.parametrize("host", list(Host))
def test_pre_adoption_worktree_stops_at_launch_and_update(
    adopted: Path,
    tmp_path: Path,
    nested: bool,
    host: Host,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    checkout = (adopted / ".worktrees" if nested else tmp_path) / "old"
    _git(adopted, "worktree", "add", "-q", "--detach", str(checkout), "before-adoption")
    child = checkout / "listener"
    child.mkdir()
    with pytest.raises(
        TjenticError, match=f"missing {host} contributor plugins"
    ) as failure:
        verify(host, child, {})
    assert "primary worktree HEAD" in str(failure.value)
    assert "python@tjentic-project, rust@tjentic-project" in str(failure.value)
    monkeypatch.chdir(child)
    assert main(["plugins", "update", "--dry-run", "--host", host]) == 1
    assert str(failure.value) in capsys.readouterr().err
    assert not (checkout / ".claude-plugin").exists()
    assert not _git(checkout, "status", "--porcelain")


def test_remote_default_with_old_primary_and_partial_host_selection(
    adopted: Path,
    tmp_path: Path,
) -> None:
    _git(adopted, "update-ref", "refs/remotes/origin/trunk", "HEAD")
    _git(
        adopted, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk"
    )
    checkout = tmp_path / "sibling"
    _git(adopted, "worktree", "add", "-q", "--detach", str(checkout))
    _git(adopted, "switch", "-q", "--detach", "before-adoption")
    _git(adopted, "branch", "-f", "trunk", "before-adoption")
    config = checkout / ".codex/config.toml"
    config.write_text(
        '[plugins."python@tjentic-project"]\nenabled = true\n', encoding="utf-8"
    )
    with pytest.raises(
        TjenticError, match="refs/remotes/origin/trunk: rust@tjentic-project"
    ) as failure:
        verify(Host.CODEX, checkout, {})
    assert "python@tjentic-project" not in str(failure.value)


def test_new_local_default_policy_is_checked_before_it_is_pushed(
    adopted: Path,
    tmp_path: Path,
) -> None:
    _git(adopted, "update-ref", "refs/remotes/origin/trunk", "before-adoption")
    _git(
        adopted, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk"
    )
    checkout = tmp_path / "sibling"
    _git(adopted, "worktree", "add", "-q", "--detach", str(checkout), "before-adoption")
    with pytest.raises(TjenticError, match="refs/heads/trunk: python@tjentic-project"):
        verify(Host.CLAUDE, checkout, {})


def test_branch_can_update_a_pin_while_keeping_the_required_selection(
    adopted: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    checkout = tmp_path / "sibling"
    _git(adopted, "worktree", "add", "-q", "--detach", str(checkout))
    market = checkout / ".claude-plugin/marketplace.json"

    catalog = json.loads(market.read_bytes())
    catalog["plugins"][0]["source"]["sha"] = "1" * 40
    market.write_text(json.dumps(catalog), encoding="utf-8")
    monkeypatch.chdir(checkout)
    for host in Host:
        assert main(["plugins", "update", "--dry-run", "--host", host]) == 0
        assert "1" * 40 in capsys.readouterr().out


def test_unrelated_project_with_no_policy_can_launch(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "--initial-branch=trunk")
    verify(Host.CODEX, tmp_path, {})
    _git(tmp_path, "commit", "--allow-empty", "-qm", "An unrelated repo")
    verify(Host.CLAUDE, tmp_path, {})


def test_corrupt_worktree_metadata_cannot_bypass_adoption_check(tmp_path: Path) -> None:
    (tmp_path / ".git").write_text("gitdir: missing/metadata\n", encoding="utf-8")
    with pytest.raises(TjenticError, match="Cannot identify the Git worktree"):
        verify(Host.CODEX, tmp_path, {})


def test_existing_root_policy_allows_intentional_subdirectory_selection(
    adopted: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    child = adopted / "python-only"
    config = child / ".codex/config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        '[plugins."rust@tjentic-project"]\nenabled = false\n', encoding="utf-8"
    )
    _git(adopted, "add", "python-only")
    _git(adopted, "commit", "-qm", "Use Python standards in this subdirectory")
    monkeypatch.chdir(child)
    assert main(["plugins", "update", "--dry-run", "--host", "codex"]) == 0
    output = capsys.readouterr().out
    assert "python@tjentic-project" in output
    assert "rust@tjentic-project" not in output
