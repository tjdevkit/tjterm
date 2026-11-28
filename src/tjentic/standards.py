"""Provision the contributor plugins selected by the current Git worktree."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from tjentic.errors import TjenticError
from tjentic.hosts.claude import plugins as claude
from tjentic.hosts.codex import plugins as codex
from tjentic.hosts.host import Host
from tjentic.locking import exclusive
from tjentic.plugins.catalog import Catalog, ReadFile, read_file
from tjentic.plugins.worktree import checked_selection


class _Adapter(Protocol):
    def selected(
        self, root: Path, directory: Path, read: ReadFile = read_file
    ) -> Catalog | None: ...

    def resolve(
        self,
        catalog: Catalog,
        directory: Path,
        environment: Mapping[str, str],
        *,
        refresh: bool,
    ) -> Catalog: ...

    def update(
        self,
        catalog: Catalog,
        directory: Path,
        plugins: list[str],
        environment: Mapping[str, str],
    ) -> None: ...

    def check_startup(
        self,
        catalog: Catalog,
        directory: Path,
        plugins: Sequence[str],
        environment: Mapping[str, str],
    ) -> None: ...

    HOST: Host


_NATIVE: tuple[_Adapter, ...] = (claude, codex)
_ADAPTERS = {adapter.HOST: adapter for adapter in _NATIVE}


def add_command(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    plugins = commands.add_parser("plugins", help="manage native contributor plugins")
    actions = plugins.add_subparsers(dest="plugins_action", required=True)
    update = actions.add_parser("update", help="install the selected plugin revisions")
    update.add_argument(
        "--dry-run", action="store_true", help="show what this checkout selects"
    )
    check = actions.add_parser(
        "check", help="check selected startup hooks before working"
    )
    for action in (update, check):
        action.add_argument(
            "--host", type=Host, choices=list(Host), help="native client"
        )


def verify(host: Host, directory: Path, environment: Mapping[str, str]) -> None:
    """Guard managed launches while leaving projects without a selection alone."""
    adapter = _ADAPTERS[host]
    catalog, plugins = checked_selection(directory, host, adapter.selected, environment)
    if catalog is not None and plugins:
        catalog.check_legacy(directory)
        catalog = adapter.resolve(
            catalog, directory.resolve(), environment, refresh=False
        )
        adapter.check_startup(catalog, directory.resolve(), plugins, environment)


def run(
    args: argparse.Namespace, directory: Path, environment: Mapping[str, str]
) -> None:
    host = args.host or Host.current(environment)
    adapter = _ADAPTERS[host]
    catalog, plugins = checked_selection(directory, host, adapter.selected, environment)
    if catalog is None:
        raise TjenticError("No contributor plugin marketplace in this Git worktree.")
    catalog.check_legacy(directory)
    if not plugins:
        print(f"No {host} contributor plugins selected in this checkout.")
        return
    if args.plugins_action == "update":
        for identity in plugins:
            publisher = catalog.marketplaces.get(identity.partition("@")[2])
            expected = catalog.revisions[identity]
            if publisher and publisher.ref:
                expected = f"marketplace ref {publisher.ref}"
            print(f"{identity} {expected or 'publisher revision'}", flush=True)
        if args.dry_run:
            return
        profile = host.profile(environment)
        profile.mkdir(parents=True, exist_ok=True)
        with exclusive(profile / "tj-plugin-update.lock"):
            catalog = adapter.resolve(
                catalog, directory.resolve(), environment, refresh=True
            )
            adapter.update(catalog, directory.resolve(), plugins, environment)
        print(f"Run tj plugins check --host {host} before starting a new session.")
    else:
        catalog = adapter.resolve(
            catalog, directory.resolve(), environment, refresh=False
        )
        adapter.check_startup(catalog, directory.resolve(), plugins, environment)
        print(f"Verified {len(plugins)} {host} contributor plugins for {directory}.")
