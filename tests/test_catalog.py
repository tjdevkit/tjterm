import json
import subprocess
from pathlib import Path

import pytest

from tjentic.errors import TjenticError
from tjentic.hosts.claude import plugins as claude
from tjentic.hosts.codex import plugins as codex
from tjentic.hosts.host import Host
from tjentic.plugins.catalog import PackageSource
from tjentic.plugins.worktree import checked_selection

SOURCE = "https://example.org/engineering/editorial.git"
PLUGIN = "voice@editorial"


def git(directory: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=test@example.org",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=directory,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def declare(root: Path, source: str, ref: str) -> None:
    (root / ".codex").mkdir(exist_ok=True)
    (root / ".claude").mkdir(exist_ok=True)
    (root / ".codex/config.toml").write_text(
        f'[marketplaces.editorial]\nsource_type="git"\nsource="{source}"\nref="{ref}"\n'
        f'[plugins."{PLUGIN}"]\nenabled=true\n[plugins."disabled@optional"]\nenabled=false\n',
        encoding="utf-8",
    )
    (root / ".claude/settings.json").write_text(
        json.dumps(
            {
                "extraKnownMarketplaces": {
                    "editorial": {
                        "source": {"source": "git", "url": source, "ref": ref}
                    }
                },
                "enabledPlugins": {PLUGIN: True, "disabled@optional": False},
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("host", list(Host))
def test_native_project_selects_a_separate_publisher_and_checks_its_source_and_pin(
    tmp_path: Path,
    host: Host,
) -> None:
    project, publisher = tmp_path / "project", tmp_path / "publisher"
    project.mkdir()
    publisher.mkdir()
    git(project, "init", "-q")
    git(publisher, "init", "-q")
    git(publisher, "remote", "add", "origin", SOURCE)
    manifest = publisher / ".claude-plugin/marketplace.json"
    manifest.parent.mkdir()
    manifest.write_text(
        json.dumps(
            {"name": "editorial", "plugins": [{"name": "voice", "source": "./"}]}
        ),
        encoding="utf-8",
    )
    git(publisher, "add", ".")
    git(publisher, "commit", "-qm", "Publish a plugin")
    revision = git(publisher, "rev-parse", "HEAD")
    declare(project, SOURCE, revision)
    select = codex.selected if host is Host.CODEX else claude.selected
    catalog, plugins = checked_selection(project, host, select, {})
    assert catalog is not None
    assert plugins == [PLUGIN]
    resolved = catalog.resolved({"editorial": manifest}, {})
    assert resolved.revisions == {PLUGIN: revision}
    assert resolved.package_source(PLUGIN) == PackageSource(publisher, "")
    with pytest.raises(TjenticError, match="Missing project marketplace editorial"):
        catalog.resolved({}, {})
    git(publisher, "remote", "set-url", "origin", "https://example.org/other.git")
    with pytest.raises(TjenticError, match="different Git source"):
        catalog.resolved({"editorial": manifest}, {})
    git(publisher, "remote", "set-url", "origin", SOURCE)
    git(publisher, "commit", "--allow-empty", "-qm", "New unselected revision")
    with pytest.raises(TjenticError, match="needs " + revision):
        catalog.resolved({"editorial": manifest}, {})


@pytest.mark.parametrize("host", list(Host))
def test_old_worktree_requires_plugin_from_external_project_marketplace(
    tmp_path: Path,
    host: Host,
) -> None:
    project = tmp_path / "default"
    project.mkdir()
    git(project, "init", "-q")
    git(project, "commit", "--allow-empty", "-qm", "Before adoption")
    sibling = tmp_path / "old"
    git(project, "worktree", "add", "--detach", str(sibling))
    declare(project, SOURCE, "main")
    git(project, "add", ".")
    git(project, "commit", "-qm", "Adopt the publisher")
    select = codex.selected if host is Host.CODEX else claude.selected
    with pytest.raises(TjenticError, match="primary worktree HEAD: voice@editorial"):
        checked_selection(sibling, host, select, {})


@pytest.mark.parametrize(
    ("source", "location", "subdir"),
    [
        ({"source": "url", "url": SOURCE}, SOURCE, ""),
        (
            {"source": "github", "repo": "editorial/rules"},
            "https://github.com/editorial/rules.git",
            "",
        ),
        (
            {
                "source": "git-subdir",
                "url": "editorial/rules",
                "path": "./plugins/voice",
            },
            "https://github.com/editorial/rules.git",
            "plugins/voice",
        ),
    ],
)
def test_in_checkout_catalog_preserves_the_published_plugin_source(
    tmp_path: Path, source: dict[str, str], location: str, subdir: str
) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/settings.json").write_text(
        json.dumps({"enabledPlugins": {PLUGIN: True}}), encoding="utf-8"
    )
    manifest = tmp_path / ".claude-plugin/marketplace.json"
    manifest.parent.mkdir()
    manifest.write_text(
        json.dumps(
            {"name": "editorial", "plugins": [{"name": "voice", "source": source}]}
        ),
        encoding="utf-8",
    )
    catalog = claude.selected(tmp_path, tmp_path)
    assert catalog is not None
    assert catalog.package_source(PLUGIN) == PackageSource(location, subdir)


@pytest.mark.parametrize("host", list(Host))
def test_project_can_enable_a_marketplace_already_known_to_the_native_host(
    tmp_path: Path, host: Host
) -> None:
    project, publisher = tmp_path / "project", tmp_path / "publisher"
    project.mkdir()
    publisher.mkdir()
    git(project, "init", "-q")
    git(publisher, "init", "-q")
    manifest = publisher / ".claude-plugin/marketplace.json"
    manifest.parent.mkdir()
    manifest.write_text(
        json.dumps(
            {"name": "editorial", "plugins": [{"name": "voice", "source": "./"}]}
        ),
        encoding="utf-8",
    )
    git(publisher, "add", ".")
    git(publisher, "commit", "-qm", "A marketplace configured in the native profile")
    (project / ".claude").mkdir()
    (project / ".claude/settings.json").write_text(
        json.dumps({"enabledPlugins": {PLUGIN: True}}), encoding="utf-8"
    )
    (project / ".codex").mkdir()
    (project / ".codex/config.toml").write_text(
        f'[plugins."{PLUGIN}"]\nenabled=true\n', encoding="utf-8"
    )
    select = codex.selected if host is Host.CODEX else claude.selected
    catalog, enabled = checked_selection(project, host, select, {})
    assert enabled == [PLUGIN]
    assert catalog is not None
    with pytest.raises(TjenticError, match="Missing project marketplace editorial"):
        catalog.resolved({}, {})
    assert catalog.resolved({"editorial": manifest}, {}).revisions == {
        PLUGIN: git(publisher, "rev-parse", "HEAD")
    }
