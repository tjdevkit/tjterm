import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from tjentic.errors import TjenticError
from tjentic.plugins.catalog import Catalog, ReadFile, read_file
from tjentic.sessions.hosts import Host
from tjentic.startup import (
    Installation,
    StartupError,
    as_object,
    as_rows,
    read_object,
    require_revision,
)

HOST = Host.CLAUDE


def selected(
    catalog: Catalog, directory: Path, read: ReadFile = read_file
) -> list[str]:
    settings = catalog.root / ".claude" / "settings.json"
    contents = read(settings)
    if contents is None:
        return []
    try:
        data = json.loads(contents.decode("utf-8-sig"))
        plugins = data.get("enabledPlugins", {})
        if not isinstance(plugins, dict):
            raise ValueError("expected an enabledPlugins table")
        return catalog.selected(
            {key for key, enabled in plugins.items() if enabled is True}
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise TjenticError(f"Invalid {settings}: {error}") from error


def update(
    catalog: Catalog,
    directory: Path,
    plugins: list[str],
    environment: Mapping[str, str],
) -> None:
    stale = [
        identity
        for identity in plugins
        if installed_revision(directory, identity, environment)
        != catalog.revisions[identity]
    ]
    if not stale:
        return

    def invoke(*args: str) -> None:
        result = subprocess.run(
            [*Host.CLAUDE.command(), "plugin", *args, "--json"],
            cwd=catalog.root,
            env=environment,
            check=False,
        )
        if result.returncode:
            raise TjenticError(f"Claude plugin command failed: {' '.join(args)}")

    invoke("marketplace", "add", str(catalog.root), "--scope", "local")
    for identity in stale:
        invoke("install", identity, "--scope", "local")
        if (
            installed_revision(directory, identity, environment)
            != catalog.revisions[identity]
        ):
            # Native update can select the local row of a different worktree.
            # Reinstall this checkout's row, retaining the plugin's data.
            invoke("uninstall", identity, "--scope", "local", "--keep-data")
            invoke("install", identity, "--scope", "local")
            if (
                installed_revision(directory, identity, environment)
                != catalog.revisions[identity]
            ):
                raise TjenticError(
                    f"Claude did not install the selected revision of {identity}."
                )


def _git_project(directory: Path) -> Path:
    project = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=directory,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    return (
        Path(project.stdout.strip()).resolve() if not project.returncode else directory
    )


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
    raise StartupError(f"Claude has no matching Git install receipt for {identity}.")


def _control(
    directory: Path, environment: Mapping[str, str], method: str
) -> dict[str, object]:
    """Ask Claude for its effective settings without authentication, hooks or a prompt."""
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


def _claude_policy(
    directory: Path, plugins: Sequence[str], environment: Mapping[str, str]
) -> None:
    for variable in ("CLAUDE_CODE_SIMPLE", "CLAUDE_CODE_SAFE_MODE"):
        if environment.get(variable, "").lower() in ("true", "on", "yes", "1"):
            raise StartupError(f"{variable} disables Claude plugin hooks.")
    policy = as_object(
        _control(directory, environment, "get_hooks_listing").get("policy")
    )
    for key in ("allDisabled", "managedOnly", "disabledByPolicy"):
        if not isinstance(policy.get(key), bool):
            raise StartupError("Claude returned an unknown hook policy.")
    if policy["disabledByPolicy"]:
        raise StartupError("Claude's managed policy disables all configured hooks.")
    if policy["allDisabled"] or policy["managedOnly"]:
        sources = as_rows(
            _control(directory, environment, "get_settings").get("sources")
        )
        forced: dict[str, object] = {}
        for source in sources:
            if source.get("source") == "policySettings":
                settings = as_object(source.get("settings"))
                forced.update(as_object(settings.get("enabledPlugins", {})))
        if any(forced.get(identity) is not True for identity in plugins):
            raise StartupError(
                "Claude's effective settings restrict the selected plugin hooks."
            )


def installed_revision(
    directory: Path, identity: str, environment: Mapping[str, str]
) -> str | None:
    rows = _claude_rows(directory, environment)
    install = _claude_installation(
        rows,
        directory,
        identity,
        _git_project(directory).parent,
        HOST.profile(environment),
    )
    return install.revision if install else None


def check_startup(
    catalog: Catalog,
    directory: Path,
    plugins: Sequence[str],
    environment: Mapping[str, str],
) -> None:
    _claude_policy(directory, plugins, environment)
    rows = _claude_rows(directory, environment)
    main_project = _git_project(directory).parent
    profile = HOST.profile(environment)
    for identity in plugins:
        install = _claude_installation(rows, directory, identity, main_project, profile)
        require_revision(
            identity, install, catalog.revisions[identity]
        ).startup_commands()
