"""Verify Claude's Git-less plugin cache against its selected Git publisher."""

import hashlib
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

from tjentic.hosts.host import Host
from tjentic.plugins.catalog import Catalog
from tjentic.plugins.tree import PackageTree, TreeError


def published_tree(
    catalog: Catalog,
    identity: str,
    revision: str,
    environment: Mapping[str, str],
    *,
    acquire: bool,
) -> PackageTree:
    if not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
        raise TreeError(f"Invalid recorded Git revision for {identity}.")
    if identity in catalog.trees:
        return catalog.trees[identity]
    source = catalog.package_source(identity)
    if source is None:
        raise TreeError(f"Missing selected Git source for {identity}.")
    if isinstance(source.location, Path):
        return PackageTree.read(
            source.location, revision, source.subdirectory, environment
        )

    ref = source.ref if catalog.revisions[identity] is None else None
    namespace = f"{source.location}\0{ref}" if ref else source.location
    key = hashlib.sha256(namespace.encode("utf-8")).hexdigest()
    store = Host.CLAUDE.profile(environment) / "tjentic" / "publisher-trees" / key
    proof = f"refs/tjentic/{revision}"

    def verified_tree() -> PackageTree:
        if ref:
            target = subprocess.run(
                [
                    "git",
                    "-c",
                    "core.longpaths=true",
                    "--no-replace-objects",
                    "rev-parse",
                    "--verify",
                    f"{proof}^{{commit}}",
                ],
                cwd=store,
                env={**environment, "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": ""},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            if target.returncode or target.stdout.strip() != revision:
                raise TreeError(
                    f"{identity} at {revision} does not match publisher ref {ref}."
                )
        return PackageTree.read(store, revision, source.subdirectory, environment)

    if (store / "HEAD").is_file():
        try:
            return verified_tree()
        except TreeError:
            pass  # Setup can reacquire an incomplete object store.
    if not acquire:
        raise TreeError(
            f"Missing publisher tree for {identity} at {revision}. "
            "Run tj plugins update --host claude."
        )
    # Keep immutable commit and tree objects. Installed command and rule bodies
    # are read by Claude from its native cache. Checks stay offline.
    store.mkdir(parents=True, exist_ok=True)
    for args in (
        ["init", "--bare", "--quiet"],
        [
            "fetch",
            "--quiet",
            "--no-tags",
            "--depth=1",
            "--filter=blob:none",
            "--",
            source.location,
            f"+{ref or revision}:{proof}",
        ],
    ):
        try:
            result = subprocess.run(
                ["git", "-c", "core.longpaths=true", "--no-replace-objects", *args],
                cwd=store,
                env={**environment, "GIT_TERMINAL_PROMPT": "0"},
                capture_output=True,
                check=False,
                timeout=90,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise TreeError(
                f"Git could not acquire the publisher tree for {identity}."
            ) from error
        if result.returncode:
            raise TreeError(
                f"Cannot acquire the selected Git tree for {identity} at {revision}."
            )
    return verified_tree()
