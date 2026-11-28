import json
import os
import subprocess
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path

from tjentic.errors import TjenticError
from tjentic.hosts.codex.server import CodexServer, codex_server
from tjentic.hosts.host import Host
from tjentic.plugins.catalog import Catalog, Marketplace, ReadFile, read_file
from tjentic.plugins.startup import (
    Installation,
    StartupError,
    as_object,
    as_rows,
    native_matcher,
    read_object,
    require_commands,
    require_revision,
)
from tjentic.plugins.tree import PackageTree, TreeError

HOST = Host.CODEX


def selected(root: Path, directory: Path, read: ReadFile = read_file) -> Catalog | None:
    enabled: dict[str, bool] = {}
    declarations: dict[str, dict[str, object]] = {}
    path = root
    for part in (".", *directory.resolve().relative_to(root).parts):
        path = path / part
        config = path / ".codex" / "config.toml"
        contents = read(config)
        if contents is not None:
            try:
                data = tomllib.loads(contents.decode("utf-8"))
                declarations.update(data.get("marketplaces", {}))
                for key, settings in data.get("plugins", {}).items():
                    if "enabled" in settings:
                        enabled[key] = settings["enabled"]
            except (AttributeError, TypeError, ValueError) as error:
                raise TjenticError(f"Invalid {config}: {error}") from error
    active = {key for key, on in enabled.items() if on is True}
    markets = {}
    for name in {key.partition("@")[2] for key in active}.intersection(declarations):
        description = declarations[name]
        try:
            source = description["source"]
            kind = description["source_type"]
            ref = description.get("ref")
            if (
                not isinstance(source, str)
                or kind not in ("git", "local")
                or (ref is not None and not isinstance(ref, str))
            ):
                raise ValueError("expected a local or Git marketplace source")
            markets[name] = Marketplace(source, ref, local=kind == "local")
        except (KeyError, TypeError, ValueError) as error:
            raise TjenticError(f"Invalid Codex marketplace {name}: {error}") from error
    return Catalog.project(
        root,
        active,
        markets,
        (
            root / ".agents/plugins/marketplace.json",
            root / ".claude-plugin/marketplace.json",
        ),
        read,
    )


def resolve(
    catalog: Catalog, directory: Path, environment: Mapping[str, str], *, refresh: bool
) -> Catalog:
    if not catalog.marketplaces:
        return catalog.resolved({}, environment)
    if refresh:
        for name, publisher in catalog.marketplaces.items():
            if publisher is None:
                continue
            if publisher.local:
                continue
            result = subprocess.run(
                [*HOST.command(), "plugin", "marketplace", "upgrade", name, "--json"],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            if result.returncode:
                raise TjenticError(
                    f"Codex cannot refresh the declared marketplace {name}. "
                    f"Check project trust and source. {result.stderr.strip()}"
                )
    with codex_server(directory, environment) as client:
        result_data = client.request(
            "plugin/list",
            {
                "cwds": [str(directory)],
                "marketplaceKinds": ["local"],
                "forceRefetch": False,
            },
        )
    manifests = {
        market: Path(path)
        for entry in as_rows(result_data.get("marketplaces"))
        if isinstance(market := entry.get("name"), str)
        and market in catalog.marketplaces
        and isinstance(path := entry.get("path"), str)
    }
    return catalog.resolved(manifests, environment)


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
            pin = catalog.revisions[identity]
            if (
                pin
                and installed_revision(
                    client, directory, identity, catalog.trees.get(identity)
                )
                == pin
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
                            catalog.manifests.get(
                                identity.partition("@")[2], catalog.manifest
                            )
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
            _selected_installation(
                client,
                catalog,
                directory,
                identity,
                _codex_hooks(client, directory),
                environment,
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


def _checkout(path: object) -> Path | None:
    if not isinstance(path, str) or not Path(path).is_absolute():
        return None
    return next(
        (root for root in Path(path).resolve().parents if (root / ".git").exists()),
        None,
    )


def _codex_installation(
    client: CodexServer,
    directory: Path,
    identity: str,
    hooks: list[dict[str, object]],
    tree: PackageTree | None = None,
) -> Installation | None:
    paths = [
        hook.get("sourcePath")
        for hook in hooks
        if hook.get("source") == "plugin" and hook.get("pluginId") == identity
    ]
    if not paths:
        skills = client.request("skills/list", {"cwds": [str(directory)]})
        for entry in as_rows(skills.get("data")):
            if entry.get("cwd") == str(directory):
                paths.extend(
                    skill.get("path")
                    for skill in as_rows(entry.get("skills"))
                    if skill.get("pluginId") == identity
                    and skill.get("enabled") is True
                )
    if not paths:
        return None
    if tree:
        return Installation(tree.installed_root(identity, paths), tree.revision)
    roots = {_checkout(path) for path in paths}
    if len(roots) != 1 or (root := roots.pop()) is None:
        raise StartupError(f"Cannot verify the installed Git checkout of {identity}.")
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
        ["git", "status", "--porcelain", "--untracked-files=all", "--", "."],
        cwd=root,
        check=False,
        capture_output=True,
    )
    revision = head.stdout.strip()
    if changed.returncode or (
        changed.stdout and not _marketplace_receipt(root, changed.stdout, revision)
    ):
        raise StartupError(
            f"Installed files of {identity} differ from its Git revision at {root}."
        )
    return Installation(root, revision)


def _marketplace_receipt(root: Path, status: bytes, revision: str) -> bool:
    # Codex copies a Git marketplace's own acquisition receipt with its root plugin.
    if status != b"?? .codex-marketplace-install.json\n":
        return False
    path = root / ".codex-marketplace-install.json"
    if path.is_symlink():
        return False
    receipt = read_object(path)
    origin = subprocess.run(
        ["git", "config", "--local", "--get", "remote.origin.url"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    sparse = receipt.get("sparse_paths")
    return (
        origin.returncode == 0
        and receipt.get("source_type") == "git"
        and receipt.get("source") == origin.stdout.strip()
        and receipt.get("revision") == revision
        and isinstance(sparse, list)
        and all(isinstance(item, str) for item in sparse)
        and (
            receipt.get("ref_name") is None or isinstance(receipt.get("ref_name"), str)
        )
    )


def _hook_manifest(root: Path) -> tuple[Path, object]:
    native = root / ".codex-plugin/plugin.json"
    shared = root / "plugin.json"
    if shared.is_file():
        package = read_object(shared)
        if (
            package.get("$schema")
            == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
        ):
            extension = as_object(package.get("extensions", {})).get("com.openai")
            if extension is not None:
                return shared, as_object(extension).get("hooks")
            overlay = read_object(native) if native.is_file() else {}
            return shared, overlay.get("hooks")
    for path in (
        native,
        root / ".claude-plugin/plugin.json",
        root / ".cursor-plugin/plugin.json",
    ):
        if path.is_file():
            return path, read_object(path).get("hooks")
    if (root / "hooks/hooks.json").is_file():
        return native, None
    raise StartupError(f"Missing Codex plugin manifest at {root}.")


def _startup_commands(install: Installation) -> list[tuple[Path, str, str]]:
    root = install.root
    manifest, declarations = _hook_manifest(root)
    commands = []
    for path, matcher, handler in install.hook_handlers(
        manifest, declarations, merge_default=False
    ):
        command = handler["command"]
        if os.name == "nt":
            command = handler.get("commandWindows", command)
        if not isinstance(command, str) or not command.strip():
            raise StartupError(f"Invalid Codex SessionStart command in {path}.")
        commands.append(
            (
                path,
                matcher,
                command.replace("${CLAUDE_PLUGIN_ROOT}", str(root)).replace(
                    "${PLUGIN_ROOT}", str(root)
                ),
            )
        )
    return commands


def _check_aliases(
    install: Installation,
    identity: str,
    selected: list[dict[str, object]],
    hooks: list[dict[str, object]],
    client: CodexServer,
    directory: Path,
    trees: Mapping[str, PackageTree],
) -> None:
    commands = {
        str(hook.get("command")).replace(str(install.root), "") for hook in selected
    }
    if not commands:
        return
    aliases: dict[str, list[dict[str, object]]] = {}
    for hook in hooks:
        other = hook.get("pluginId")
        if (
            hook.get("eventName") == "sessionStart"
            and hook.get("source") == "plugin"
            and hook.get("enabled") is True
            and hook.get("trustStatus") in ("trusted", "managed")
            and isinstance(other, str)
            and other != identity
        ):
            aliases.setdefault(other, []).append(hook)
    for other, candidates in aliases.items():
        tree = trees.get(identity) or trees.get(other)
        root: Path | None
        if tree:
            try:
                root = tree.installed_root(
                    other, [hook.get("sourcePath") for hook in candidates]
                )
            except TreeError:
                continue
        else:
            root = _checkout(candidates[0].get("sourcePath"))
            if root is None:
                continue
        if any(
            str(hook.get("command")).replace(str(root), "") in commands
            for hook in candidates
        ):
            alias = (
                Installation(root, tree.revision)
                if tree
                else _codex_installation(client, directory, other, candidates)
            )
            install.check_alias(identity, other, alias, tree=tree)


def _selected_installation(
    client: CodexServer,
    catalog: Catalog,
    directory: Path,
    identity: str,
    hooks: list[dict[str, object]],
    environment: Mapping[str, str],
) -> Installation:
    pin = catalog.revisions[identity]
    install = require_revision(
        identity,
        _codex_installation(
            client, directory, identity, hooks, catalog.trees.get(identity)
        ),
        pin,
    )
    source = catalog.package_source(identity)
    if pin is None and source and isinstance(source.location, str):
        # Native cache keys identify the plugin and marketplace, not their publisher.
        Marketplace(source.location, source.ref).checkout_revision(
            identity, install.root, directory, environment
        )
    return install


def check_startup(
    catalog: Catalog,
    directory: Path,
    plugins: Sequence[str],
    environment: Mapping[str, str],
) -> None:
    with codex_server(directory, environment) as client:
        result = client.request("config/read", {"cwd": str(directory)})
        features = as_object(as_object(result.get("config")).get("features") or {})
        if features.get("plugins") is False:
            raise StartupError("Enable Codex plugins in this checkout.")
        inventory = client.request("plugin/installed", {"cwds": [str(directory)]})
        active = set()
        for market in as_rows(inventory.get("marketplaces")):
            for entry in as_rows(market.get("plugins")):
                if entry.get("installed") is True and entry.get("enabled") is True:
                    active.add(str(entry.get("id")))
        hooks = _codex_hooks(client, directory)
        for identity in plugins:
            if identity not in active:
                raise StartupError(
                    f"Codex plugin {identity} is missing or disabled in {directory}."
                )
            install = _selected_installation(
                client, catalog, directory, identity, hooks, environment
            )
            expected = _startup_commands(install)
            if expected and features.get("hooks") is False:
                raise StartupError("Enable Codex hooks in this checkout.")
            selected = _for_plugin(hooks, identity)
            require_commands(
                identity,
                expected,
                (
                    (
                        Path(str(hook.get("sourcePath"))).resolve(),
                        native_matcher(hook.get("matcher")),
                        str(hook.get("command")),
                    )
                    for hook in selected
                ),
            )
            _check_trust(identity, selected)
            _check_aliases(
                install, identity, selected, hooks, client, directory, catalog.trees
            )


def _check_trust(identity: str, selected: list[dict[str, object]]) -> None:
    for hook in selected:
        if hook.get("enabled") is not True or hook.get("trustStatus") not in (
            "trusted",
            "managed",
        ):
            raise StartupError(
                f"{identity} hook is disabled or {hook.get('trustStatus')}. "
                "Open Codex here and review the selected hooks in /hooks."
            )
        if hook.get("handlerType") != "command" or hook.get("async") is not False:
            raise StartupError(
                f"Unexpected Codex SessionStart registration for {identity}."
            )


def installed_revision(
    client: CodexServer, directory: Path, identity: str, tree: PackageTree | None = None
) -> str | None:
    try:
        install = _codex_installation(
            client, directory, identity, _codex_hooks(client, directory), tree
        )
    except TreeError:
        # The update path replaces a stale or modified native copy.
        return None
    return install.revision if install else None
