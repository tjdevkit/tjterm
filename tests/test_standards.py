import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from tjentic.cli import main
from tjentic.sessions.hosts import Host


@pytest.mark.parametrize("host", list(Host))
def test_managed_launcher_selects_its_host_over_inherited_session_ids(
    host: Host,
) -> None:
    environment = {other.session_variable: "inherited" for other in Host}
    assert Host.current({**environment, "TJ_HOST": host.value}) is host


def test_project_plugins_enable_pinned_python_and_rust_in_each_host() -> None:
    root = Path(__file__).resolve().parents[1]
    catalog = json.loads(
        (root / ".claude-plugin/marketplace.json").read_text(encoding="utf-8")
    )
    assert catalog["name"] == "tjentic-project"
    plugins = catalog["plugins"]
    assert sorted(plugin["name"] for plugin in plugins) == ["python", "rust"]
    for plugin in plugins:
        name = plugin["name"]
        source = plugin["source"]
        assert source["source"] == "url"
        assert source["url"] == f"https://github.com/tjentic/{name}-standards.git"
        assert re.fullmatch("[0-9a-f]{40}", source["sha"])

    expected = {f"{plugin['name']}@{catalog['name']}" for plugin in plugins}
    claude = json.loads((root / ".claude/settings.json").read_text(encoding="utf-8"))
    codex = tomllib.loads((root / ".codex/config.toml").read_text(encoding="utf-8"))
    assert {
        key for key, active in claude["enabledPlugins"].items() if active is True
    } == expected
    assert {
        key for key, entry in codex["plugins"].items() if entry.get("enabled") is True
    } == expected


def test_native_selection_from_linked_checkout_preserves_instruction_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    primary = tmp_path / "project"
    root = Path(__file__).resolve().parents[1]
    subprocess.run(["git", "init", "-q", str(primary)], check=True)
    for folder in (".claude-plugin", ".claude", ".codex"):
        shutil.copytree(root / folder, primary / folder)
    notes = b"# Team notes\n"
    (primary / "AGENTS.md").write_bytes(notes)
    subprocess.run(["git", "add", "."], cwd=primary, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "Adopt plugins",
        ],
        cwd=primary,
        check=True,
    )
    for directory in (primary / ".worktrees" / "nested", tmp_path / "sibling"):
        subprocess.run(
            ["git", "worktree", "add", "-q", "--detach", str(directory)],
            cwd=primary,
            check=True,
        )
        child = directory / "listener"
        child.mkdir()
        instructions = directory / "AGENTS.md"
        original = instructions.read_bytes()
        monkeypatch.chdir(child)
        for host in ("codex", "claude"):
            assert main(["plugins", "update", "--dry-run", "--host", host]) == 0
            assert "rust@tjentic-project" in capsys.readouterr().out
        assert instructions.read_bytes() == original
        instructions.write_bytes(
            original + b"<!-- tjentic:standards:start -->\nold rules\n"
            b"<!-- tjentic:standards:end -->\n"
        )
        assert main(["plugins", "check", "--host", "codex"]) == 1
        assert "marked standards block" in capsys.readouterr().err


def test_old_selection_reports_migration_without_rewriting_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".tjentic").mkdir()
    (tmp_path / ".tjentic/standards.toml").write_text(
        "standards = []", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    assert main(["plugins", "update"]) == 1
    assert "Migrate" in capsys.readouterr().err
    assert not (tmp_path / "AGENTS.md").exists()
