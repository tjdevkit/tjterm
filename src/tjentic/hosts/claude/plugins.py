import json
import stat
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

from tjentic.errors import TjenticError
from tjentic.hosts.claude.trees import published_tree
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
from tjentic.plugins.tree import TreeError

HOST = Host.CLAUDE


def selected(root: Path, directory: Path, read: ReadFile = read_file) -> Catalog | None:
    settings = root / ".claude" / "settings.json"
    contents = read(settings)
    try:
        data = json.loads(contents.decode("utf-8-sig")) if contents else {}
        plugins = data.get("enabledPlugins", {})
        if not isinstance(plugins, dict):
            raise ValueError("expected an enabledPlugins table")
        active = {key for key, enabled in plugins.items() if enabled is True}
        declarations = data.get("extraKnownMarketplaces", {})
        markets = {}
        for name in {key.partition("@")[2] for key in active}.intersection(
            declarations
        ):
            source = declarations[name]["source"]
            kind = source["source"]
            if kind == "github":
                url = f"https://github.com/{source['repo']}.git"
            elif kind == "git":
                url = source["url"]
            elif kind == "directory":
                url = source["path"]
            else:
                raise ValueError(f"unsupported marketplace source {kind} for {name}")
            markets[name] = Marketplace(
                url, source.get("ref"), local=kind == "directory"
            )
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise TjenticError(f"Invalid {settings}: {error}") from error
    return Catalog.project(
        root, active, markets, (root / ".claude-plugin/marketplace.json",), read
    )


def _plugin_command(
    directory: Path, environment: Mapping[str, str], *args: str
) -> None:
    result = subprocess.run(
        [*HOST.command(), "plugin", *args, "--json"],
        cwd=directory,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode:
        raise TjenticError(
            f"Claude plugin command failed: {' '.join(args)}. {result.stderr.strip()}"
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
            source = publisher.source
            if publisher.local:
                source = str((catalog.root / source).resolve())
            elif publisher.ref:
                source = f"{source}#{publisher.ref}"
            _plugin_command(
                catalog.root,
                environment,
                "marketplace",
                "add",
                source,
                "--scope",
                "local",
            )
            if not publisher.local:
                _plugin_command(
                    catalog.root, environment, "marketplace", "update", name
                )
    registry = HOST.profile(environment) / "plugins" / "known_marketplaces.json"
    known = read_object(registry) if registry.is_file() else {}
    manifests = {}
    for name in catalog.marketplaces:
        entry = known.get(name)
        if isinstance(entry, dict) and isinstance(
            path := entry.get("installLocation"), str
        ):
            manifests[name] = Path(path) / ".claude-plugin/marketplace.json"
    return catalog.resolved(manifests, environment)


def update(
    catalog: Catalog,
    directory: Path,
    plugins: list[str],
    environment: Mapping[str, str],
) -> None:
    def ready(identity: str) -> bool:
        pin = catalog.revisions[identity]
        installed = installation(directory, identity, environment)
        if installed is None or (pin and installed.revision != pin):
            return False
        tree = published_tree(
            catalog, identity, installed.revision, environment, acquire=True
        )
        try:
            tree.verify(installed.root, native_metadata=_lease)
        except TreeError:
            return False
        return True

    stale = [
        identity
        for identity in plugins
        if not catalog.revisions[identity] or not ready(identity)
    ]
    if not stale:
        return

    def invoke(*args: str) -> None:
        _plugin_command(catalog.root, environment, *args)

    if any(
        identity.partition("@")[2] not in catalog.marketplaces for identity in stale
    ):
        invoke("marketplace", "add", str(catalog.root), "--scope", "local")
    for identity in stale:
        pin = catalog.revisions[identity]
        invoke("install", identity, "--scope", "local")
        if pin is None or not ready(identity):
            # A native registration can retain a correct SHA for a damaged cache.
            # Reinstall for this checkout, retaining the plugin's user data.
            invoke("uninstall", identity, "--scope", "local", "--keep-data")
            invoke("install", identity, "--scope", "local")
        if not ready(identity):
            raise TjenticError(
                f"Claude did not install the selected revision of {identity}."
            )


def _primary_worktree(directory: Path) -> Path:
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain", "-z"],
        cwd=directory,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    record = result.stdout.partition("\0")[0]
    if result.returncode or not record.startswith("worktree "):
        raise StartupError(
            f"Git could not identify the primary worktree for {directory}."
        )
    return Path(record.removeprefix("worktree ")).resolve()


def _claude_rows(
    directory: Path, environment: Mapping[str, str]
) -> list[dict[str, object]]:
    try:
        result = subprocess.run(
            [*Host.CLAUDE.command(), "plugin", "list", "--json"],
            cwd=directory,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=30,
            env=environment,
        )
    except subprocess.TimeoutExpired as error:
        raise StartupError(f"Claude plugin list timed out in {directory}.") from error
    if result.returncode:
        raise StartupError(f"Claude could not list plugins in {directory}.")
    try:
        return as_rows(json.loads(result.stdout))
    except ValueError as error:
        raise StartupError("Invalid Claude plugin inventory.") from error


def _claude_installation(
    rows: list[dict[str, object]],
    directory: Path,
    identity: str,
    main_project: Path,
    profile: Path,
    *,
    selected: bool = True,
) -> Installation | None:
    applicable: list[tuple[tuple[int, int], dict[str, object]]] = []
    for row in rows:
        if row.get("id") != identity:
            continue
        scope = row.get("scope")
        rank = {"managed": 4, "local": 3, "project": 2, "user": 1}.get(str(scope))
        if rank is None:
            raise StartupError(
                f"Unknown Claude installation scope {scope} for {identity}."
            )
        depth = 0
        if scope in ("project", "local"):
            scoped = row.get("projectPath")
            if not isinstance(scoped, str):
                continue
            project = Path(scoped).resolve()
            if directory.is_relative_to(project):
                depth = len(project.parts)
            elif project != main_project:
                continue
        applicable.append(((rank, depth), row))
    if not applicable:
        return None
    best = max(rank for rank, row in applicable)
    matches = [row for rank, row in applicable if rank == best]
    if len(matches) != 1:
        raise StartupError(
            f"Ambiguous Claude installation of {identity} in {directory}."
        )
    row = matches[0]
    if row.get("enabled") is not True:
        if not selected:
            return None
        raise StartupError(f"Enable {identity} in Claude for {directory}.")
    registry = as_object(
        read_object(profile / "plugins" / "installed_plugins.json").get("plugins")
    )
    for entry in as_rows(registry.get(identity)):
        if all(
            entry.get(key) == row.get(key)
            for key in ("scope", "installPath", "projectPath")
        ):
            revision, path = entry.get("gitCommitSha"), entry.get("installPath")
            if isinstance(revision, str) and isinstance(path, str):
                return Installation(Path(path), revision)
    if not selected:
        return None
    raise StartupError(f"Claude has no matching Git install receipt for {identity}.")


def _control(
    directory: Path, environment: Mapping[str, str], method: str
) -> dict[str, object]:
    """Read effective policy without authentication, hooks or a model prompt."""
    request = {
        "type": "control_request",
        "request_id": "tj",
        "request": {"subtype": method},
    }
    try:
        result = subprocess.run(
            [
                *HOST.command(),
                "--bare",
                "-p",
                "--no-session-persistence",
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
                "--verbose",
            ],
            input=json.dumps(request) + "\n",
            cwd=directory,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=20,
        )
        for line in result.stdout.splitlines():
            message = as_object(json.loads(line))
            if message.get("type") == "control_response":
                response = as_object(message.get("response"))
                if (
                    response.get("request_id") == "tj"
                    and response.get("subtype") == "success"
                ):
                    return as_object(response.get("response"))
    except (subprocess.TimeoutExpired, OSError, ValueError) as error:
        raise StartupError(f"Cannot read Claude's native {method} response.") from error
    raise StartupError(f"Claude did not report {method} in {directory}.")


def _claude_hooks(
    directory: Path, plugins: Sequence[str], environment: Mapping[str, str]
) -> list[dict[str, object]]:
    for variable in ("CLAUDE_CODE_SIMPLE", "CLAUDE_CODE_SAFE_MODE"):
        if environment.get(variable, "").lower() in ("true", "on", "yes", "1"):
            raise StartupError(f"{variable} disables Claude plugin hooks.")
    listing = _control(directory, environment, "get_hooks_listing")
    policy = as_object(listing.get("policy"))
    for key in ("allDisabled", "managedOnly", "disabledByPolicy"):
        if not isinstance(policy.get(key), bool):
            raise StartupError("Claude returned an unknown hook policy.")
    if plugins and policy["disabledByPolicy"]:
        raise StartupError("Claude's managed policy disables all configured hooks.")
    forced: dict[str, object] | None = None
    if policy["allDisabled"] or policy["managedOnly"]:
        sources = as_rows(
            _control(directory, environment, "get_settings").get("sources")
        )
        forced = {}
        for source in sources:
            if source.get("source") == "policySettings":
                settings = as_object(source.get("settings"))
                forced.update(as_object(settings.get("enabledPlugins", {})))
        if any(forced.get(identity) is not True for identity in plugins):
            raise StartupError(
                "Claude's effective settings restrict the selected plugin hooks."
            )
    return [
        hook
        for hook in as_rows(listing.get("hooks"))
        if hook.get("source") == "pluginHook"
        and hook.get("event") == "SessionStart"
        and (forced is None or forced.get(str(hook.get("pluginName"))) is True)
    ]


def _lease(root: Path, path: Path) -> bool:
    # Claude records active package users alongside the installed files.
    relative = path.relative_to(root)
    if relative.parts == (".in_use",):
        return stat.S_ISDIR(path.lstat().st_mode)
    if (
        relative.parent != Path(".in_use")
        or not relative.name.isascii()
        or not relative.name.isdecimal()
    ):
        return False
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return False
        lease = json.loads(path.read_bytes())
    except FileNotFoundError:
        return True  # The native process may already have released this lease.
    except ValueError:
        return False
    if not isinstance(lease, dict):
        return False
    return (
        str(lease.get("pid")) == relative.name
        and set(lease) <= {"pid", "procStart"}
        and ("procStart" not in lease or str(lease["procStart"]).isdecimal())
    )


def installation(
    directory: Path, identity: str, environment: Mapping[str, str]
) -> Installation | None:
    return _claude_installation(
        _claude_rows(directory, environment),
        directory,
        identity,
        _primary_worktree(directory),
        HOST.profile(environment),
    )


def startup_handlers(
    install: Installation,
) -> Iterator[tuple[Path, str, dict[str, object]]]:
    manifest = install.root / ".claude-plugin/plugin.json"
    declarations = read_object(manifest).get("hooks") if manifest.is_file() else None
    if not manifest.is_file() and not (install.root / "hooks/hooks.json").is_file():
        raise StartupError(f"Missing Claude plugin manifest at {install.root}.")
    return install.hook_handlers(manifest, declarations, merge_default=True)


def _startup_commands(install: Installation) -> list[tuple[str, str]]:
    commands = []
    for path, matcher, handler in startup_handlers(install):
        args = handler.get("args", [])
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise StartupError(
                f"Invalid Claude SessionStart command arguments in {path}."
            )
        # The native hook inventory renders exec-form command and arguments as text.
        commands.append((matcher, " ".join((str(handler["command"]), *args))))
    return commands


def check_startup(
    catalog: Catalog,
    directory: Path,
    plugins: Sequence[str],
    environment: Mapping[str, str],
) -> None:
    rows = _claude_rows(directory, environment)
    main_project = _primary_worktree(directory)
    profile = HOST.profile(environment)
    installations = {}
    for identity in plugins:
        install = _claude_installation(rows, directory, identity, main_project, profile)
        selected_install = require_revision(
            identity, install, catalog.revisions[identity]
        )
        tree = published_tree(
            catalog, identity, selected_install.revision, environment, acquire=False
        )
        tree.verify(selected_install.root, native_metadata=_lease)
        installations[identity] = (
            selected_install,
            _startup_commands(selected_install),
        )
    required = [
        identity for identity, (_, commands) in installations.items() if commands
    ]
    hooks = _claude_hooks(directory, required, environment)
    for identity, (selected_install, expected) in installations.items():
        selected = [hook for hook in hooks if hook.get("pluginName") == identity]
        require_commands(
            identity,
            expected,
            (
                (native_matcher(hook.get("matcher")), str(hook.get("commandText")))
                for hook in selected
                if hook.get("type") == "command"
            ),
        )
        commands = {str(hook.get("commandText")) for hook in selected}
        aliases = {
            other
            for hook in hooks
            if isinstance(other := hook.get("pluginName"), str)
            and hook.get("commandText") in commands
            and other != identity
        }
        for other in aliases:
            alias = _claude_installation(
                rows, directory, other, main_project, profile, selected=False
            )
            selected_install.check_alias(
                identity,
                other,
                alias,
                tree=catalog.trees.get(identity) or catalog.trees.get(other),
                native_metadata=_lease,
            )
