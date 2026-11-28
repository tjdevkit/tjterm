import json
import subprocess
import tomllib
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path

from tjentic.errors import TjenticError
from tjentic.plugins.catalog import Catalog, ReadFile, read_file
from tjentic.sessions.hosts import Host
from tjentic.startup import (
    CodexServer,
    Installation,
    StartupError,
    as_object,
    as_rows,
    codex_server,
    require_revision,
)

HOST = Host.CODEX


def selected(
    catalog: Catalog, directory: Path, read: ReadFile = read_file
) -> list[str]:
    # Read the checkout selection even when the plugin is missing from this profile.
    enabled: dict[str, bool] = {}
    path = catalog.root
    for part in (".", *directory.resolve().relative_to(path).parts):
        path = path / part
        config = path / ".codex" / "config.toml"
        contents = read(config)
        if contents is not None:
            try:
                plugins = tomllib.loads(contents.decode("utf-8")).get("plugins", {})
                for key, settings in plugins.items():
                    if "enabled" in settings:
                        enabled[key] = settings["enabled"]
            except (AttributeError, TypeError, ValueError) as error:
                raise TjenticError(f"Invalid {config}: {error}") from error
    return catalog.selected({key for key, active in enabled.items() if active is True})


def update(
    catalog: Catalog,
    directory: Path,
    plugins: list[str],
    environment: Mapping[str, str],
) -> None:
    """Install from this worktree's marketplace, preserving account-wide enablement."""
    config = HOST.profile(environment) / "config.toml"
    with codex_server(directory, environment) as client:
        for identity in plugins:
            if (
                installed_revision(client, directory, identity)
                == catalog.revisions[identity]
            ):
                continue
            defaults = {}
            if config.is_file():
                with config.open("rb") as stream:
                    defaults = tomllib.load(stream).get("plugins", {})
            enabled = defaults.get(identity, {}).get("enabled", False)
            if not isinstance(enabled, bool):
                raise TjenticError(f"Invalid user setting for {identity} in {config}.")
            try:
                client.request(
                    "plugin/install",
                    {
                        "marketplacePath": str(
                            catalog.root / ".claude-plugin" / "marketplace.json"
                        ),
                        "pluginName": identity.partition("@")[0],
                    },
                )
            finally:
                # Native installation also enables the user-wide default.
                # The tracked project setting owns activation in this checkout.
                if not enabled:
                    # The install server may have exited after writing the setting.
                    with codex_server(directory, environment) as settings:
                        settings.request(
                            "config/value/write",
                            {
                                "keyPath": f"plugins.{json.dumps(identity)}.enabled",
                                "value": False,
                                "mergeStrategy": "upsert",
                            },
                        )
            if (
                installed_revision(client, directory, identity)
                != catalog.revisions[identity]
            ):
                raise TjenticError(
                    f"Codex did not install the selected revision of {identity}."
                )


def _codex_hooks(client: CodexServer, directory: Path) -> list[dict[str, object]]:
    result = client.request("hooks/list", {"cwds": [str(directory)]})
    entries = [
        row for row in as_rows(result.get("data")) if row.get("cwd") == str(directory)
    ]
    if len(entries) != 1 or entries[0].get("errors") != []:
        raise StartupError(f"Codex could not enumerate hooks for {directory}.")
    return as_rows(entries[0].get("hooks"))


def _for_plugin(
    hooks: list[dict[str, object]], identity: str
) -> list[dict[str, object]]:
    return [
        hook
        for hook in hooks
        if hook.get("source") == "plugin"
        and hook.get("pluginId") == identity
        and hook.get("eventName") == "sessionStart"
    ]


def _codex_installation(
    hooks: list[dict[str, object]], identity: str
) -> Installation | None:
    if not hooks:
        return None
    path = hooks[0].get("sourcePath")
    if not isinstance(path, str) or any(
        hook.get("sourcePath") != path for hook in hooks
    ):
        raise StartupError(f"Ambiguous SessionStart source for {identity}.")
    manifest = Path(path)
    root = manifest.parent.parent
    if manifest != root / "hooks" / "hooks.json" or not (root / ".git").exists():
        raise StartupError(
            f"Cannot verify the installed Git checkout of {identity} at {root}."
        )
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if head.returncode:
        raise StartupError(
            f"Cannot read the installed Git revision of {identity} at {root}."
        )
    changed = subprocess.run(
        ["git", "diff", "--no-ext-diff", "--quiet", "HEAD", "--", "."],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if changed.returncode:
        raise StartupError(
            f"Installed files of {identity} differ from its Git revision at {root}."
        )
    return Installation(root, head.stdout.strip())


def check_startup(
    catalog: Catalog,
    directory: Path,
    plugins: Sequence[str],
    environment: Mapping[str, str],
) -> None:
    with codex_server(directory, environment) as client:
        result = client.request("config/read", {"cwd": str(directory)})
        features = as_object(as_object(result.get("config")).get("features") or {})
        if features.get("hooks") is False or features.get("plugins") is False:
            raise StartupError("Enable Codex hooks and plugins in this checkout.")
        hooks = _codex_hooks(client, directory)
    for identity in plugins:
        selected = _for_plugin(hooks, identity)
        install = require_revision(
            identity,
            _codex_installation(selected, identity),
            catalog.revisions[identity],
        )
        commands = install.startup_commands()
        expected = Counter(
            command.replace("${CLAUDE_PLUGIN_ROOT}", str(install.root)).replace(
                "${PLUGIN_ROOT}", str(install.root)
            )
            for command in commands
        )
        actual = Counter(str(hook.get("command")) for hook in selected)
        if actual != expected:
            raise StartupError(
                f"Incomplete Codex SessionStart registration for {identity}."
            )
        for hook in selected:
            if hook.get("enabled") is not True or hook.get("trustStatus") not in (
                "trusted",
                "managed",
            ):
                raise StartupError(
                    f"{identity} hook is disabled or {hook.get('trustStatus')}. "
                    "Open Codex here and review the selected hooks in /hooks."
                )
            if (
                hook.get("handlerType") != "command"
                or hook.get("async") is not False
                or hook.get("matcher") not in (None, "", "*")
            ):
                raise StartupError(
                    f"Unexpected Codex SessionStart registration for {identity}."
                )


def installed_revision(
    client: CodexServer, directory: Path, identity: str
) -> str | None:
    hooks = _for_plugin(_codex_hooks(client, directory), identity)
    install = _codex_installation(hooks, identity)
    return install.revision if install else None
