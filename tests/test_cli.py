import argparse
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from tjentic.cli import main
from tjentic.watch.native import Watch
from tjentic.workflows import runner
from tjentic.workflows.runner import bind_worktree
from tjentic.workflows.store import database

FIRST = "019a0000-0000-7000-8000-000000000001"
SECOND = "019a0000-0000-7000-8000-000000000002"
FAKE = Path(__file__).parent / "fixtures" / "codex.py"


def thread(session_id: str, cwd: Path) -> dict[str, object]:
    return {
        "id": session_id,
        "cwd": str(cwd),
        "name": "Review widgets",
        "preview": "First prompt",
        "updatedAt": 1735689600,
    }


def run_recovery(
    directory: Path, scenario: Path, config: dict[str, object], *options: str
) -> subprocess.CompletedProcess[str]:
    scenario.write_text(json.dumps(config), encoding="utf-8")
    launcher = (
        "import sys; from tjentic.cli import main; "
        "sys.exit(main(sys.argv[3:], codex_command=[sys.executable, *sys.argv[1:3]]))"
    )
    return subprocess.run(
        [sys.executable, "-c", launcher, str(FAKE), str(scenario), "recover", *options],
        cwd=directory,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )


def messages(scenario: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in scenario.with_suffix(".log")
        .read_text(encoding="utf-8")
        .splitlines()
    ]


def test_search_across_linked_worktrees_paginates_native_results(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "project"
    linked = tmp_path / "project topic"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "Initial",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(linked)],
        check=True,
    )
    scenario = tmp_path / "pages.json"
    result = run_recovery(
        repo,
        scenario,
        {
            "query": "assistant phrase",
            "pages": [[thread(FIRST, tmp_path)], [thread(SECOND, linked)]],
        },
        "assistant phrase",
        "--json",
        "--limit",
        "1",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        {
            "provider": "codex",
            "id": SECOND,
            "cwd": str(linked),
            "title": "Review widgets",
            "updated_at": 1735689600,
            "directory_exists": True,
        }
    ]
    assert [row["method"] for row in messages(scenario)] == [
        "initialize",
        "initialized",
        "thread/search",
        "thread/search",
    ]


def test_default_listing_never_resumes_and_cleans_terminal_text(tmp_path: Path) -> None:
    scenario = tmp_path / "lines.json"
    record = thread(FIRST, tmp_path / "old-worktree")
    record["name"] = "Review\x1b[31m\nwidgets"
    result = run_recovery(tmp_path, scenario, {"pages": [[record]]}, "--all")
    assert result.returncode == 0, result.stderr
    assert FIRST in result.stdout and "[directory missing]" in result.stdout
    assert "\x1b" not in result.stdout
    assert [row["method"] for row in messages(scenario)] == [
        "initialize",
        "initialized",
        "thread/list",
    ]


def test_resume_hands_exact_id_and_directory_to_native_writer(tmp_path: Path) -> None:
    recorded = tmp_path / "work tree"
    recorded.mkdir()
    scenario = tmp_path / "resume.json"
    result = run_recovery(
        tmp_path,
        scenario,
        {"pages": [[thread(FIRST, recorded)]], "exit_code": 23},
        "--resume",
        FIRST,
    )
    assert result.returncode == 23, result.stderr
    commands = messages(scenario)
    assert commands[-2]["params"] == {
        "currentAccountOnly": True,
        "modelProviders": [],
        "includedThreadIds": [FIRST],
        "limit": 1,
    }
    assert commands[-1] == {
        "argv": ["resume", "--no-daemon", "--cd", str(recorded), FIRST],
        "cwd": str(recorded),
    }


@pytest.mark.allow_hosts(["127.0.0.1"])
@pytest.mark.parametrize(
    ("command", "resume"), [("codex", False), ("codex", True), ("recover", True)]
)
def test_native_launch_checks_its_checkout_before_acquiring_a_writer(
    command: str,
    resume: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recorded = tmp_path / "saved"
    subprocess.run(["git", "init", "-q", str(recorded)], check=True)
    (recorded / ".tjentic").mkdir()
    (recorded / ".tjentic" / "standards.toml").write_text("", encoding="utf-8")
    scenario = tmp_path / "resume.json"
    scenario.write_text(
        json.dumps({"pages": [[thread(FIRST, recorded)]]}), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path if resume else recorded)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(Watch, "call", lambda *_args: "")
    assert (
        main(
            [command, *(["--resume", FIRST] if resume else [])],
            codex_command=[sys.executable, str(FAKE), str(scenario)],
        )
        == 1
    )
    assert ".tjentic/standards.toml" in capsys.readouterr().err
    expected = {"initialize", "initialized"}
    if resume:
        expected.add("thread/list")
    assert {row["method"] for row in messages(scenario)} == expected


@pytest.mark.parametrize(
    "case", ["missing", "unknown", "different", "duplicate", "old-server"]
)
def test_failed_recovery_never_launches_a_session(tmp_path: Path, case: str) -> None:
    config: dict[str, object]
    if case == "old-server":
        config = {"error": "unsupported field in this Codex version"}
    elif case == "missing":
        config = {"pages": [[thread(FIRST, tmp_path / "gone")]]}
    elif case == "different":
        config = {"pages": [[thread(SECOND, tmp_path)]]}
    elif case == "duplicate":
        config = {"pages": [[thread(FIRST, tmp_path), thread(FIRST, tmp_path)]]}
    else:
        config = {"pages": [[]]}
    scenario = tmp_path / "failed.json"
    result = run_recovery(tmp_path, scenario, config, "--resume", FIRST)
    assert result.returncode == 1
    assert result.stderr.startswith("tj: ")
    assert "argv" not in messages(scenario)[-1]


def test_invalid_uuid_is_refused_before_contacting_codex() -> None:
    with pytest.raises(SystemExit, match="2"):
        main(["recover", "--resume", "--last"])


@pytest.mark.allow_hosts(["127.0.0.1"])
def test_workflow_status_reuses_the_previous_preview_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    worktree = tmp_path / "worktree"
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)
    monkeypatch.chdir(worktree)
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("LOCALAPPDATA", str(state))
    with database(state / "tjterm") as db:
        bind_worktree(db, "existing item", worktree)

    assert main(["workflow", "status", "existing item"]) == 0
    assert json.loads(capsys.readouterr().out)["name"] == "existing item"
    assert not (state / "tjentic").exists()


@pytest.mark.allow_hosts(["127.0.0.1"])
def test_workflow_uses_the_available_lane_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    seen: list[Path] = []

    async def capture_configuration(
        args: argparse.Namespace,
        command: Sequence[str],
        state: Path,
        config: Path,
        *,
        environment: Mapping[str, str],
    ) -> int:
        assert environment["XDG_CONFIG_HOME"] == str(tmp_path)
        seen.append(config)
        return 0

    monkeypatch.setattr(runner, "workflow", capture_configuration)
    previous = tmp_path / "tjterm" / "lanes.toml"
    current = tmp_path / "tjentic" / "lanes.toml"
    assert main(["workflow", "status", "some item"]) == 0
    previous.parent.mkdir()
    previous.touch()
    assert main(["workflow", "status", "some item"]) == 0
    current.parent.mkdir()
    current.touch()
    assert main(["workflow", "status", "some item"]) == 0
    assert seen == [current, previous, current]


@pytest.mark.parametrize(
    ("command", "provider"), [("claude", "Claude Code"), ("codex", "Codex")]
)
def test_launcher_finds_its_own_provider(
    command: str,
    provider: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    assert main([command]) == 1
    assert f"Install the native {provider} CLI" in capsys.readouterr().err
