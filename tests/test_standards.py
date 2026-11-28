import json
import subprocess
from pathlib import Path

import pytest

from tjentic.cli import main
from tjentic.hosts.host import Host


@pytest.mark.parametrize("host", list(Host))
def test_managed_launcher_selects_its_host_over_inherited_session_ids(
    host: Host,
) -> None:
    environment = {other.session_variable: "inherited" for other in Host}
    assert Host.current({**environment, "TJ_HOST": host.value}) is host


def test_each_host_uses_its_enabled_publisher_from_the_checkout(
    editorial: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(editorial)
    (editorial / ".claude/settings.json").write_text(
        json.dumps({"enabledPlugins": {"voice@editorial": True}}), encoding="utf-8"
    )
    (editorial / ".codex/config.toml").write_text(
        '[plugins."captions@editorial"]\nenabled = true\n', encoding="utf-8"
    )
    for host, name, pin in [
        ("claude", "voice", "2" * 40),
        ("codex", "captions", "1" * 40),
    ]:
        assert main(["plugins", "update", "--host", host, "--dry-run"]) == 0
        assert capsys.readouterr().out.splitlines() == [f"{name}@editorial {pin}"]


def test_native_selection_from_linked_checkout_preserves_instruction_files(
    editorial: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    primary = editorial
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
            output = capsys.readouterr().out.splitlines()
            assert [row.split()[0] for row in output] == [
                "captions@editorial",
                "voice@editorial",
            ]
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
