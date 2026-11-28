import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tjentic.errors import TjenticError
from tjentic.hosts.claude import plugins as claude
from tjentic.hosts.claude.trees import published_tree
from tjentic.hosts.codex import plugins as codex
from tjentic.hosts.codex.server import codex_server
from tjentic.hosts.host import Host
from tjentic.standards import verify

IDENTITY = "team@project"


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
    save(plugin / ".claude-plugin/plugin.json", {"name": "team"})
    git(plugin, "init", "-q")
    git(plugin, "add", ".")
    git(plugin, "commit", "-qm", "plugin")
    save(
        project / ".claude-plugin" / "marketplace.json",
        {
            "name": "project",
            "plugins": [
                {
                    "name": "team",
                    "source": {
                        "source": "url",
                        "url": plugin.as_uri(),
                        "sha": git(plugin, "rev-parse", "HEAD"),
                    },
                },
            ],
        },
    )
    save(project / ".claude" / "settings.json", {"enabledPlugins": {IDENTITY: True}})
    (project / ".codex").mkdir()
    (project / ".codex" / "config.toml").write_text(
        '[plugins."team@project"]\nenabled=true\n', encoding="utf-8"
    )
    return project


def environment(checkout: Path) -> dict[str, str]:
    profile = checkout.parent / "profile"
    system = {
        key: os.environ[key]
        for key in (
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
            "TMP",
            "TEMP",
            "TMPDIR",
        )
        if key in os.environ
    }
    return {
        **system,
        "HOME": str(profile),
        "CLAUDE_CONFIG_DIR": str(profile),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ALLOW_PROTOCOL": "file",
    }


def codex_fixture(
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    registered: list[tuple[Path, str]] | None = None,
    skill: Path | None = None,
    matcher: str | None = None,
) -> None:
    plugin = checkout.parent / "plugin"
    if registered is None:
        registered = [
            (plugin / "hooks/hooks.json", word) for word in ("first", "second")
        ]
    rows = [
        {
            "key": name,
            "command": name,
            "source": "plugin",
            "pluginId": IDENTITY,
            "eventName": "sessionStart",
            "sourcePath": str(path),
            "handlerType": "command",
            "enabled": mode != "disabled",
            "async": False,
            "matcher": matcher,
            "trustStatus": mode if mode in ("untrusted", "modified") else "trusted",
        }
        for path, name in registered
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
            "plugin/installed": {
                "marketplaces": [
                    {
                        "plugins": [
                            {
                                "id": IDENTITY,
                                "installed": True,
                                "enabled": mode != "plugin-off",
                            }
                        ]
                    }
                ]
            },
            "skills/list": {
                "data": [
                    {
                        "cwd": str(checkout),
                        "skills": (
                            [
                                {
                                    "pluginId": IDENTITY,
                                    "enabled": True,
                                    "path": str(skill),
                                }
                            ]
                            if skill is not None
                            else []
                        ),
                    }
                ]
            },
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
        ("plugin-off", "disabled"),
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
    commands: tuple[str, ...] = ("first", "second"),
    matcher: str = "",
) -> None:
    plugin = checkout.parent / "plugin"
    installed = checkout.parent / "native-claude-cache"
    shutil.copytree(
        plugin, installed, ignore=shutil.ignore_patterns(".git"), dirs_exist_ok=True
    )
    catalog = claude.selected(checkout, checkout)
    assert catalog is not None
    published_tree(
        catalog,
        IDENTITY,
        git(plugin, "rev-parse", "HEAD"),
        environment(checkout),
        acquire=True,
    )
    rows = [
        {
            "id": IDENTITY,
            "scope": "local",
            "enabled": True,
            "projectPath": str(project),
            "installPath": str(installed),
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
                    "matcher": matcher,
                    "commandText": word,
                }
                for word in commands
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


def repin(checkout: Path) -> None:
    publisher = checkout.parent / "plugin"
    git(publisher, "add", ".")
    git(publisher, "commit", "-qm", "publish new layout")
    catalog = checkout / ".claude-plugin/marketplace.json"
    document = json.loads(catalog.read_text(encoding="utf-8"))
    document["plugins"][0]["source"]["sha"] = git(publisher, "rev-parse", "HEAD")
    save(catalog, document)


@pytest.mark.parametrize(
    "registered_source", ["declared", "wrong", "partial", "untrusted"]
)
def test_codex_uses_its_declared_hook_sources(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, registered_source: str
) -> None:
    publisher = checkout.parent / "plugin"
    primary = publisher / ".codex-plugin/hooks.json"
    other = publisher / "native/company/start.json"
    custom = '"$PLUGIN_ROOT"/read company-policy'
    paths = [primary, other]
    save(
        publisher / ".codex-plugin/plugin.json",
        {
            "name": "team",
            "hooks": [f"./{path.relative_to(publisher).as_posix()}" for path in paths],
        },
    )
    for path, command in ((primary, custom), (other, "other")):
        save(
            path,
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": command}]}
                    ]
                }
            },
        )
    repin(checkout)
    path = publisher / "hooks/hooks.json" if registered_source == "wrong" else primary
    native = [(path, custom), (other, "other")]
    mode = "untrusted" if registered_source == "untrusted" else "trusted"
    codex_fixture(
        checkout,
        monkeypatch,
        mode,
        native[:1] if registered_source == "partial" else native,
    )
    if registered_source == "declared":
        verify(Host.CODEX, checkout, environment(checkout))
    else:
        with pytest.raises(TjenticError, match=r"untrusted|Incomplete"):
            verify(Host.CODEX, checkout, environment(checkout))


def test_claude_verifies_exec_arguments_and_merged_inline_hooks(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = checkout.parent / "plugin"
    hook = {
        "type": "command",
        "command": "${CLAUDE_PLUGIN_ROOT}/reader",
        "args": ["rules for this team"],
    }
    save(
        publisher / ".claude-plugin/plugin.json",
        {"name": "team", "hooks": {"SessionStart": [{"hooks": [hook]}]}},
    )
    repin(checkout)
    for command in (
        "${CLAUDE_PLUGIN_ROOT}/reader rules for this team",
        "${CLAUDE_PLUGIN_ROOT}/reader",
    ):
        claude_fixture(
            checkout, monkeypatch, [checkout], commands=("first", "second", command)
        )
        if command.endswith("team"):
            verify(Host.CLAUDE, checkout, environment(checkout))
        else:
            with pytest.raises(TjenticError, match="Incomplete"):
                verify(Host.CLAUDE, checkout, environment(checkout))


@pytest.mark.parametrize("host", list(Host))
def test_skill_only_install_is_verified_without_a_startup_hook(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, host: Host
) -> None:
    publisher = checkout.parent / "plugin"
    (publisher / "hooks/hooks.json").unlink()
    (publisher / "hooks").rmdir()
    skill = publisher / "skills/team/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("A team skill.", encoding="utf-8")
    repin(checkout)
    if host is Host.CODEX:
        codex_fixture(checkout, monkeypatch, "trusted", [], skill)
    else:
        claude_fixture(checkout, monkeypatch, [checkout], commands=())
    verify(host, checkout, environment(checkout))
    if host is Host.CODEX:
        codex_fixture(checkout, monkeypatch, "trusted", [])
    else:
        claude_fixture(checkout, monkeypatch, [], commands=())
    with pytest.raises(TjenticError, match="missing"):
        verify(host, checkout, environment(checkout))


@pytest.mark.parametrize(
    "receipt_state", ["native", "wrong revision", "extra untracked"]
)
def test_codex_validates_native_marketplace_acquisition_receipt(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, receipt_state: str
) -> None:
    plugin = checkout.parent / "plugin"
    source = "https://registry.invalid/team.git"
    git(plugin, "remote", "add", "origin", source)
    revision = git(plugin, "rev-parse", "HEAD")
    save(
        plugin / ".codex-marketplace-install.json",
        {
            "source_type": "git",
            "source": source,
            "ref_name": None,
            "sparse_paths": [],
            "revision": "0" * 40 if receipt_state == "wrong revision" else revision,
        },
    )
    if receipt_state == "extra untracked":
        (plugin / "unexpected").write_text("extra", encoding="utf-8")
    codex_fixture(checkout, monkeypatch, "trusted")
    if receipt_state == "native":
        verify(Host.CODEX, checkout, environment(checkout))
    else:
        with pytest.raises(TjenticError, match="differ from its Git revision"):
            verify(Host.CODEX, checkout, environment(checkout))


@pytest.mark.parametrize("host", list(Host))
@pytest.mark.parametrize("registered", ["startup", "resume"])
def test_preflight_preserves_the_publishers_session_matcher(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, host: Host, registered: str
) -> None:
    publisher = checkout.parent / "plugin"
    path = publisher / "hooks/hooks.json"
    package = json.loads(path.read_text(encoding="utf-8"))
    package["hooks"]["SessionStart"][0]["matcher"] = "startup"
    save(path, package)
    git(publisher, "commit", "-qam", "Run the publisher commands on a new session")
    market = checkout / ".claude-plugin/marketplace.json"
    selection = json.loads(market.read_text(encoding="utf-8"))
    selection["plugins"][0]["source"]["sha"] = git(publisher, "rev-parse", "HEAD")
    save(market, selection)
    if host is Host.CODEX:
        codex_fixture(checkout, monkeypatch, "trusted", matcher=registered)
    else:
        claude_fixture(checkout, monkeypatch, [checkout], matcher=registered)
    if registered == "startup":
        verify(host, checkout, environment(checkout))
    else:
        with pytest.raises(TjenticError, match="Incomplete native SessionStart"):
            verify(host, checkout, environment(checkout))


def test_claude_blocks_missing_default_hook_from_pinned_install(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude_fixture(checkout, monkeypatch, [checkout], commands=())
    (checkout.parent / "native-claude-cache/hooks/hooks.json").unlink()
    with pytest.raises(TjenticError, match="Incomplete installed plugin"):
        verify(Host.CLAUDE, checkout, environment(checkout))


def test_floating_codex_package_requires_the_declared_remote_and_ref(
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "https://example.org/team/selected.git"
    plugin = checkout.parent / "plugin"
    git(plugin, "branch", "-M", "main")
    git(plugin, "remote", "add", "origin", "https://example.org/another.git")
    manifest = checkout / ".claude-plugin/marketplace.json"
    market = json.loads(manifest.read_bytes())
    entry = {"source": "url", "url": source, "ref": "release"}
    market["plugins"][0]["source"] = entry
    save(manifest, market)
    codex_fixture(checkout, monkeypatch, "trusted")
    with pytest.raises(TjenticError, match="different Git source"):
        verify(Host.CODEX, checkout, environment(checkout))
    git(plugin, "remote", "set-url", "origin", source)
    with pytest.raises(TjenticError, match="needs release"):
        verify(Host.CODEX, checkout, environment(checkout))
    git(plugin, "tag", "release", "HEAD")
    verify(Host.CODEX, checkout, environment(checkout))
