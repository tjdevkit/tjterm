import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from tjterm.cli import main


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Standards fixture",
            "-c",
            "user.email=fixture@example.com",
            "-c",
            "commit.gpgsign=false",
            *arguments,
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


@pytest.fixture
def source(tmp_path: Path) -> Path:
    repository = tmp_path / "standard source"
    repository.mkdir()
    _git(repository, "init", "--template=")
    return repository


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "worktree"
    root.mkdir()
    _git(root, "init", "--template=")
    (root / ".tjentic").mkdir()
    monkeypatch.chdir(root)
    return root


def _commit(repository: Path, body: str) -> str:
    (repository / "rules.md").write_text(body, encoding="utf-8")
    _git(repository, "add", "rules.md")
    _git(repository, "commit", "-m", "Publish fixture rule")
    return _git(repository, "rev-parse", "HEAD")


def _configure(
    project: Path, source: Path, revision: str, scopes: Sequence[str]
) -> None:
    sections = [
        "\n".join(
            (
                "[[standards]]",
                f"source = {json.dumps(str(source))}",
                f"revision = {json.dumps(revision)}",
                f"scope = {json.dumps(scope)}",
                'files = ["rules.md"]',
            )
        )
        for scope in scopes
    ]
    (project / ".tjentic" / "standards.toml").write_text(
        "\n\n".join(sections) if sections else "standards = []",
        encoding="utf-8",
    )


def test_sync_previews_exact_commit_bytes_and_preserves_existing_guidance(
    project: Path, source: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    older = _commit(source, "# House rules\nUse the pinned convention.\n")
    newer = _commit(source, "# House rules\nUse the revised convention.\n")
    instructions = project / "AGENTS.md"
    team = b"# Team\r\nKeep B\xc3\xa9la's notes.\r\n"
    instructions.write_bytes(team)
    claude = project / "CLAUDE.md"
    claude.write_text("@AGENTS.md\n", encoding="utf-8")
    _configure(project, source, older, ["."])

    assert main(["plugins", "update", "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "+++ b/AGENTS.md" in output
    assert "+Use the pinned convention." in output
    assert "revised" not in output
    assert instructions.read_bytes() == team

    assert main(["plugins", "update"]) == 0
    first = instructions.read_bytes()
    assert first.startswith(team)
    assert b"Use the pinned convention.\r\n" in first
    assert claude.read_text(encoding="utf-8") == "@AGENTS.md\n"
    capsys.readouterr()
    assert main(["plugins", "update"]) == 0
    assert capsys.readouterr().out == "Project standards are up to date.\n"
    assert instructions.read_bytes() == first

    _configure(project, source, newer, ["."])
    assert main(["plugins", "update"]) == 0
    assert b"Use the revised convention.\r\n" in instructions.read_bytes()
    assert b"Use the pinned convention." not in instructions.read_bytes()
    assert instructions.read_bytes().startswith(team)


def test_sync_removes_dropped_root_and_nested_sections(
    project: Path, source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = _commit(source, "Scope-specific policy.\n")
    subtree = project / "crates with spaces"
    subtree.mkdir()
    root_agents = project / "AGENTS.md"
    scoped_agents = subtree / "AGENTS.md"
    root_agents.write_bytes(b"Root guidance\n")
    scoped_agents.write_bytes(b"Scoped guidance\n")
    _configure(project, source, revision, [".", subtree.name])
    monkeypatch.chdir(subtree)

    assert main(["plugins", "update"]) == 0
    assert b"Scope-specific policy." in root_agents.read_bytes()
    assert b"Scope-specific policy." in scoped_agents.read_bytes()

    _configure(project, source, revision, [])
    assert main(["plugins", "update"]) == 0
    assert root_agents.read_bytes() == b"Root guidance\n"
    assert scoped_agents.read_bytes() == b"Scoped guidance\n"


def test_sync_rejects_invalid_scope_before_writing(
    project: Path, source: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    revision = _commit(source, "Scope-specific policy.\n")
    _configure(project, source, revision, [".", "../other"])
    assert main(["plugins", "update"]) == 1
    assert "worktree scope" in capsys.readouterr().err
    assert not (project / "AGENTS.md").exists()


def test_sync_refuses_incomplete_markers_without_writing(
    project: Path, source: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    revision = _commit(source, "Scope-specific policy.\n")
    _configure(project, source, revision, ["."])
    path = project / "AGENTS.md"
    original = b"# Team\n<!-- tjentic:standards:start -->\n"
    path.write_bytes(original)
    assert main(["plugins", "update"]) == 1
    assert "Fix the standards markers" in capsys.readouterr().err
    assert path.read_bytes() == original
