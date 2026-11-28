from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from tjentic.errors import TjenticError
from tjentic.plugins.tree import PackageTree

type ReadFile = Callable[[Path], bytes | None]


def read_file(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


@dataclass(frozen=True, slots=True)
class Marketplace:
    source: str
    ref: str | None = None
    local: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("expected a marketplace source")
        if self.ref is not None and (not isinstance(self.ref, str) or not self.ref):
            raise ValueError("expected a Git ref")

    def checkout_revision(
        self, name: str, checkout: Path, project: Path, environment: Mapping[str, str]
    ) -> str:
        if self.local:
            if checkout.resolve() != (project / self.source).resolve():
                raise TjenticError(
                    f"Native marketplace {name} uses a different directory."
                )
        else:
            origin = _git(
                checkout, environment, "config", "--local", "--get", "remote.origin.url"
            )
            if _source_url(origin) != _source_url(self.source):
                raise TjenticError(
                    f"The native checkout for {name} uses a different Git source. "
                    "Run tj plugins update here."
                )
        head = _git(checkout, environment, "rev-parse", "--verify", "HEAD")
        if self.ref:
            if re.fullmatch(r"[0-9a-fA-F]{40}", self.ref):
                matching = [self.ref.lower()]
            else:
                refs = (
                    [self.ref]
                    if self.ref.startswith("refs/")
                    else [
                        f"refs/heads/{self.ref}",
                        f"refs/remotes/origin/{self.ref}",
                        f"refs/tags/{self.ref}",
                    ]
                )
                found = _git(
                    checkout,
                    environment,
                    "for-each-ref",
                    "--format=%(refname) %(objectname) %(*objectname)",
                    "--",
                    *refs,
                )
                matching = []
                for line in found.splitlines():
                    reference, *oids = line.split()
                    if reference in refs:
                        matching.extend(oids)
            if head not in matching:
                raise TjenticError(
                    f"Marketplace {name} needs {self.ref}, found {head}. "
                    "Run tj plugins update here."
                )
        return head


@dataclass(frozen=True, slots=True)
class PackageSource:
    location: str | Path
    subdirectory: str
    ref: str | None = None


@dataclass(frozen=True, slots=True)
class _Manifest:
    path: Path
    name: str
    entries: dict[str, object]

    @classmethod
    def parse(cls, path: Path, contents: bytes) -> _Manifest:
        try:
            data = json.loads(contents.decode("utf-8-sig"))
            name = data["name"]
            if not isinstance(name, str) or not isinstance(data["plugins"], list):
                raise ValueError("expected a marketplace name and plugin list")
            entries: dict[str, object] = {}
            for plugin in data["plugins"]:
                identity = f"{plugin['name']}@{name}"
                if identity in entries:
                    raise ValueError(f"duplicate marketplace entry for {identity}")
                entries[identity] = plugin["source"]
        except (KeyError, TypeError, ValueError) as error:
            raise TjenticError(f"Invalid {path}: {error}") from error
        return cls(path, name, entries)

    def package_source(self, identity: str, checkout: Path) -> PackageSource | None:
        source = self.entries[identity]
        location: str | Path = checkout
        ref = None
        if isinstance(source, str):
            subdirectory = source
        elif isinstance(source, dict):
            kind = source.get("source")
            if kind == "github":
                repo = source.get("repo")
                if not isinstance(repo, str) or "/" not in repo:
                    raise TjenticError(f"Expected a GitHub repository for {identity}.")
                location = f"https://github.com/{repo.removesuffix('.git')}.git"
            elif kind in ("git", "url", "git-subdir"):
                url = source.get("url")
                if not isinstance(url, str) or not url:
                    raise TjenticError(f"Expected a Git source URL for {identity}.")
                if kind == "git-subdir" and re.fullmatch(r"[\w.-]+/[\w.-]+", url):
                    url = f"https://github.com/{url.removesuffix('.git')}.git"
                location = url
            elif kind != "local":
                return None
            ref = source.get("ref")
            if ref is not None and (not isinstance(ref, str) or not ref):
                raise TjenticError(f"Expected a Git ref for {identity} in {self.path}.")
            subdirectory = source.get("path", "")
        else:
            return None
        if not isinstance(subdirectory, str):
            raise TjenticError(
                f"Expected a publisher-relative plugin path for {identity}."
            )
        path = PurePosixPath(subdirectory)
        if path.is_absolute() or ".." in path.parts or "\\" in subdirectory:
            raise TjenticError(
                f"Expected a publisher-relative plugin path for {identity}."
            )
        return PackageSource(location, path.as_posix() if path.parts else "", ref)

    def revision(self, identity: str, head: str | None = None) -> str | None:
        if identity not in self.entries:
            raise TjenticError(
                f"Missing native marketplace entry for {identity} in {self.path}."
            )
        source = self.entries[identity]
        if isinstance(source, str) or (
            isinstance(source, dict) and source.get("source") == "local"
        ):
            return head
        if not isinstance(source, dict):
            raise TjenticError(
                f"Unsupported plugin source for {identity} in {self.path}."
            )
        pin = source.get("sha")
        if pin is not None:
            if not isinstance(pin, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", pin):
                raise TjenticError(
                    f"{identity} needs an exact Git commit in {self.path}."
                )
            return pin.lower()
        ref = source.get("ref")
        if isinstance(ref, str) and re.fullmatch(r"[0-9a-fA-F]{40}", ref):
            return ref.lower()
        if source.get("source") not in ("url", "git", "github", "git-subdir"):
            raise TjenticError(
                f"Select a Git-backed source for {identity} in {self.path}."
            )
        return None


@dataclass(frozen=True, slots=True)
class Catalog:
    root: Path
    manifest: Path
    revisions: dict[str, str | None]
    marketplaces: dict[str, Marketplace | None]
    manifests: dict[str, Path]
    sources: dict[str, PackageSource]
    trees: dict[str, PackageTree]

    @classmethod
    def project(
        cls,
        root: Path,
        enabled: set[str],
        publishers: dict[str, Marketplace],
        manifests: tuple[Path, ...],
        read: ReadFile,
    ) -> Catalog | None:
        # A repository can publish a marketplace independently of its contributor setup.
        # Only parse its contents when the project actually selects a plugin from it.
        manifest = None
        for local_path in manifests:
            contents = read(local_path)
            if contents is not None:
                if any(key.partition("@")[2] not in publishers for key in enabled):
                    manifest = _Manifest.parse(local_path, contents)
                break
        else:
            if not enabled:
                return None
        revisions: dict[str, str | None] = {}
        markets: dict[str, Marketplace | None] = {}
        sources = {}
        for identity in sorted(enabled):
            plugin_name, separator, market = identity.partition("@")
            if not separator or not plugin_name or not market or "@" in market:
                raise TjenticError(f"Expected a native plugin@marketplace: {identity}.")
            if market in publishers:
                markets[market] = publishers[market]
                revisions[identity] = None
            elif manifest and market == manifest.name:
                revisions[identity] = manifest.revision(identity)
                if source := manifest.package_source(identity, root):
                    sources[identity] = source
            else:
                # An enabled ID can refer to a marketplace already known to this host.
                markets[market] = None
                revisions[identity] = None
        return cls(root, local_path, revisions, markets, {}, sources, {})

    def resolved(
        self, manifests: Mapping[str, Path], environment: Mapping[str, str]
    ) -> Catalog:
        """Verify the native Git snapshot for each external project marketplace."""
        revisions = self.revisions.copy()
        trees = {}
        sources = self.sources.copy()
        for identity, package in sources.items():
            if isinstance(package.location, Path) and package.subdirectory:
                head = _git(
                    package.location, environment, "rev-parse", "--verify", "HEAD"
                )
                revisions[identity] = head
                trees[identity] = PackageTree.read(
                    package.location, head, package.subdirectory, environment
                )
        for name, publisher in self.marketplaces.items():
            manifest = manifests.get(name)
            if manifest is None or not manifest.is_file():
                raise TjenticError(
                    f"Missing project marketplace {name}. Run tj plugins update here."
                )
            checkout = manifest.parents[1]
            if manifest.parent.name == "plugins":
                checkout = manifest.parents[2]
            head = (
                publisher.checkout_revision(name, checkout, self.root, environment)
                if publisher
                else _git(checkout, environment, "rev-parse", "--verify", "HEAD")
            )
            _git(
                checkout,
                environment,
                "ls-files",
                "--error-unmatch",
                "--",
                str(manifest),
            )
            _git(
                checkout,
                environment,
                "diff",
                "--no-ext-diff",
                "--quiet",
                "HEAD",
                "--",
                str(manifest),
            )
            contents = read_file(manifest)
            if contents is None:
                raise TjenticError(f"Missing native marketplace {name} at {manifest}.")
            published = _Manifest.parse(manifest, contents)
            if published.name != name:
                raise TjenticError(f"Expected native marketplace {name} at {manifest}.")
            for identity in revisions:
                if identity.partition("@")[2] == name:
                    revisions[identity] = published.revision(identity, head)
                    if source := published.package_source(identity, checkout):
                        sources[identity] = source
                        if isinstance(source.location, Path) and source.subdirectory:
                            trees[identity] = PackageTree.read(
                                source.location, head, source.subdirectory, environment
                            )
        return replace(
            self,
            revisions=revisions,
            manifests=dict(manifests),
            sources=sources,
            trees=trees,
        )

    def package_source(self, identity: str) -> PackageSource | None:
        return self.sources.get(identity)

    def check_legacy(self, directory: Path) -> None:
        """Detect the old updater's instructions so the same rules cannot load twice."""
        current = directory.resolve()
        while current.is_relative_to(self.root):
            for name in ("AGENTS.md", "AGENTS.override.md", "CLAUDE.md"):
                path = current / name
                if (
                    path.is_file()
                    and b"<!-- tjentic:standards:start -->" in path.read_bytes()
                ):
                    raise TjenticError(
                        f"Remove the marked standards block from {path}. "
                        "The selected plugins deliver these rules at SessionStart."
                    )
            if current == self.root:
                break
            current = current.parent


def _source_url(url: str) -> str:
    for prefix in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
        if url.startswith(prefix):
            return "https://github.com/" + url.removeprefix(prefix).removesuffix(".git")
    return url


def _git(directory: Path, environment: Mapping[str, str], *args: str) -> str:
    result = subprocess.run(
        ["git", "--no-replace-objects", *args],
        cwd=directory,
        env={**environment, "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": ""},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode:
        raise TjenticError(
            f"Cannot verify the native marketplace checkout at {directory}. "
            "Run tj plugins update here."
        )
    return result.stdout.strip()
