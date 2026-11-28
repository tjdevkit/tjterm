import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tjentic.plugins.startup import Installation, StartupError
from tjentic.plugins.tree import PackageTree, TreeError


def test_native_subdirectory_copy_requires_the_exact_committed_package(
    tmp_path: Path,
) -> None:
    publisher = tmp_path / "publisher"
    package = publisher / "plugins" / "sample"
    hook = package / "hooks" / "hooks.json"
    hook.parent.mkdir(parents=True)
    hook.write_bytes(b'{"hooks":{"SessionStart":[]}}\n')
    command = package / "run"
    command.write_bytes(b"#!/bin/sh\necho sample\n")
    command.chmod(0o755)
    if os.name != "nt":
        (package / "target").symlink_to("run")

    def git(*args: str) -> str:
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
            cwd=publisher,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()

    git("init", "-q")
    git("add", ".")
    git("commit", "-qm", "Publish package")
    revision = git("rev-parse", "HEAD")
    assert "plugins/sample/run" in PackageTree.read(publisher, revision, "", {}).entries
    tree = PackageTree.read(publisher, revision, "plugins/sample", {})
    installed = tmp_path / "native-cache"
    shutil.copytree(package, installed, symlinks=True)
    source = installed / "hooks/hooks.json"
    assert tree.installed_root("sample@registry", [str(source)]) == installed
    own = Installation(installed, revision)
    alias = tmp_path / "other-cache"
    shutil.copytree(package, alias, symlinks=True)
    with pytest.raises(StartupError, match="both register"):
        own.check_alias(
            "sample@registry",
            "sample@another",
            Installation(alias, revision),
            tree=tree,
        )
    # A sibling with the same hook text and Git commit can carry different rules.
    (alias / "run").write_text("different package", encoding="utf-8")
    own.check_alias(
        "sample@registry", "sibling@registry", Installation(alias, revision), tree=tree
    )
    source.write_bytes(source.read_bytes().replace(b"\n", b"\r\n"))
    with pytest.raises(TreeError, match="differs"):
        tree.verify(installed)
    shutil.copy2(hook, source)
    extra = installed / ".extra"
    extra.touch()
    with pytest.raises(TreeError, match="differs"):
        tree.verify(installed)
    extra.unlink()
    if os.name != "nt":
        (installed / "run").chmod(0o644)
        with pytest.raises(TreeError, match="differs"):
            tree.verify(installed)
        (installed / "run").chmod(0o755)
        (installed / "target").unlink()
        (installed / "target").symlink_to("hooks/hooks.json")
        with pytest.raises(TreeError, match="differs"):
            tree.verify(installed)
        (installed / "target").unlink()
        (installed / "target").symlink_to("run")
    source.unlink()
    with pytest.raises(TreeError, match="Incomplete"):
        tree.verify(installed)
