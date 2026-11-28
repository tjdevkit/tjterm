import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tjentic.errors import TjenticError
from tjentic.plugins import codex
from tjentic.sessions.hosts import Host
from tjentic.standards import verify
from tjentic.startup import codex_server

IDENTITY = "rust@project"


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def git(directory: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.org",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        cwd=directory,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    git(project, "init", "-q")
    plugin = tmp_path / "plugin"
    handlers = [{"type": "command", "command": word} for word in ("first", "second")]
    save(
        plugin / "hooks" / "hooks.json",
        {"hooks": {"SessionStart": [{"hooks": handlers}]}},
    )
    git(plugin, "init", "-q")
    git(plugin, "add", ".")
    git(plugin, "commit", "-qm", "plugin")
    save(
        project / ".claude-plugin" / "marketplace.json",
        {
            "name": "project",
            "plugins": [
                {"name": "rust", "source": {"sha": git(plugin, "rev-parse", "HEAD")}},
            ],
        },
    )
    save(project / ".claude" / "settings.json", {"enabledPlugins": {IDENTITY: True}})
    (project / ".codex").mkdir()
    (project / ".codex" / "config.toml").write_text(
        '[plugins."rust@project"]\nenabled=true\n', encoding="utf-8"
    )
    return project


def environment(checkout: Path) -> dict[str, str]:
    return {"CLAUDE_CONFIG_DIR": str(checkout.parent / "profile"), "PATH": os.defpath}


def codex_fixture(
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    plugin = checkout.parent / "plugin"
    rows = [
        {
            "key": name,
            "command": name,
            "source": "plugin",
            "pluginId": IDENTITY,
            "eventName": "sessionStart",
            "sourcePath": str(plugin / "hooks" / "hooks.json"),
            "handlerType": "command",
            "enabled": mode != "disabled",
            "async": False,
            "matcher": None,
            "trustStatus": mode if mode in ("untrusted", "modified") else "trusted",
        }
        for name in ("first", "second")
    ]
    if mode == "partial":
        rows.pop()
    if mode == "alias":
        rows.extend([{**row, "pluginId": "legacy@other"} for row in rows])
    source = checkout.parent / "codex.py"
    source.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "for line in sys.stdin:\n"
        " m = json.loads(line)\n"
        " if 'id' not in m: continue\n"
        " result = json.loads(Path('wire.json').read_text())[m['method']]\n"
        " print(json.dumps({'id': m['id'], 'result': result}),flush=True)\n",
        encoding="utf-8",
    )
    save(
        checkout / "wire.json",
        {
            "initialize": {},
            "config/read": {"config": {"features": {"hooks": mode != "off"}}},
            "hooks/list": {
                "data": [{"cwd": str(checkout), "hooks": rows, "errors": []}]
            },
        },
    )
    monkeypatch.setattr(Host, "command", lambda host: [sys.executable, str(source)])


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("modified", "modified"),
        ("untrusted", "untrusted"),
        ("disabled", "disabled"),
        ("partial", "Incomplete"),
        ("off", "Enable"),
        ("alias", "both register"),
    ],
)
def test_check_startup_rejects_inactive_codex_hooks(
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    message: str,
) -> None:
    codex_fixture(checkout, monkeypatch, mode)
    with codex_server(checkout, environment(checkout)) as server:
        assert codex.installed_revision(server, checkout, IDENTITY) == git(
            checkout.parent / "plugin", "rev-parse", "HEAD"
        )
    with pytest.raises(TjenticError, match=message):
        verify(Host.CODEX, checkout, environment(checkout))


def test_check_startup_accepts_complete_trusted_codex_hooks(
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_fixture(checkout, monkeypatch, "trusted")
    verify(Host.CODEX, checkout, environment(checkout))


def claude_fixture(
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    projects: list[Path],
    policy: str = "enabled",
) -> None:
    plugin = checkout.parent / "plugin"
    rows = [
        {
            "id": IDENTITY,
            "scope": "local",
            "enabled": True,
            "projectPath": str(project),
            "installPath": str(plugin),
        }
        for project in projects
    ]
    save(
        checkout.parent / "profile" / "plugins" / "installed_plugins.json",
        {
            "plugins": {
                IDENTITY: [
                    {
                        **row,
                        "gitCommitSha": git(plugin, "rev-parse", "HEAD")
                        if row["projectPath"] == str(checkout)
                        else "0" * 40,
                    }
                    for row in rows
                ]
            },
        },
    )
    modes: dict[str, dict[str, object]] = {
        "get_hooks_listing": {
            "hooks": [
                {
                    "source": "pluginHook",
                    "event": "SessionStart",
                    "type": "command",
                    "pluginName": IDENTITY,
                    "matcher": "",
                    "commandText": word,
                }
                for word in ("first", "second")
            ],
            "policy": {
                "allDisabled": policy == "allDisabled",
                "managedOnly": policy == "managedOnly",
                "disabledByPolicy": False,
            },
        },
        "get_settings": {"sources": []},
    }
    if policy == "partial":
        modes["get_hooks_listing"]["hooks"] = []
    source = checkout.parent / "claude.py"
    source.write_text(
        "import sys,json\n"
        "if '--bare' in sys.argv:\n"
        " request = json.loads(sys.stdin.readline())\n"
        f" result={modes!r}[request['request']['subtype']]\n"
        " response={'request_id':'tj','subtype':'success','response':result}\n"
        " print(json.dumps({'type':'control_response','response':response}))\n"
        f"else: print({json.dumps(rows)!r})\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(Host, "command", lambda host: [sys.executable, str(source)])


@pytest.mark.parametrize(
    ("setting", "message"),
    [
        ("allDisabled", "restrict"),
        ("managedOnly", "restrict"),
        ("partial", "Incomplete"),
    ],
)
def test_check_startup_blocks_restricted_claude_hooks(
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    message: str,
) -> None:
    claude_fixture(checkout, monkeypatch, [checkout], setting)
    with pytest.raises(TjenticError, match=message):
        verify(Host.CLAUDE, checkout, environment(checkout))


def test_check_startup_rejects_another_claude_project(
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = checkout.parent / "unrelated"
    other.mkdir()
    claude_fixture(checkout, monkeypatch, [other])
    with pytest.raises(TjenticError, match="missing"):
        verify(Host.CLAUDE, checkout, environment(checkout))


def test_check_startup_matches_exact_linked_claude_installation(
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git(checkout, "add", ".")
    git(checkout, "commit", "-qm", "project")
    linked = checkout.parent / "sibling"
    git(checkout, "worktree", "add", "--detach", str(linked))
    claude_fixture(linked, monkeypatch, [checkout, linked])
    verify(Host.CLAUDE, linked, environment(linked))


def test_check_startup_rejects_modified_codex_package(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_fixture(checkout, monkeypatch, "trusted")
    manifest = checkout.parent / "plugin" / "hooks" / "hooks.json"
    manifest.write_text("{}", encoding="utf-8")
    with pytest.raises(TjenticError, match="differ from its Git revision"):
        verify(Host.CODEX, checkout, environment(checkout))
