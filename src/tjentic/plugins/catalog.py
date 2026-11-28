from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from tjentic.errors import TjenticError

type ReadFile = Callable[[Path], bytes | None]


def read_file(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


@dataclass(frozen=True, slots=True)
class Catalog:
    root: Path
    name: str
    revisions: dict[str, str]

    @classmethod
    def read(cls, root: Path, read: ReadFile = read_file) -> Catalog | None:
        manifest = root / ".claude-plugin" / "marketplace.json"
        contents = read(manifest)
        if contents is None:
            return None
        try:
            data = json.loads(contents.decode("utf-8-sig"))
            name = data["name"]
            plugins = data["plugins"]
            if not isinstance(name, str) or not isinstance(plugins, list):
                raise ValueError("expected a marketplace name and plugin list")
            revisions: dict[str, str] = {}
            for plugin in plugins:
                source = plugin["source"]
                if not isinstance(source, dict) or "sha" not in source:
                    continue
                identity = f"{plugin['name']}@{name}"
                pin = source["sha"]
                if not isinstance(pin, str) or not re.fullmatch("[0-9a-fA-F]{40}", pin):
                    raise ValueError(f"{identity} needs an exact Git commit")
                if identity in revisions:
                    raise ValueError(f"duplicate plugin {identity}")
                revisions[identity] = pin.lower()
        except (KeyError, ValueError, TypeError) as error:
            raise TjenticError(f"Invalid {manifest}: {error}") from error
        return cls(root, name, revisions)

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

    def selected(self, enabled: set[str]) -> list[str]:
        selected = sorted(key for key in enabled if key.endswith(f"@{self.name}"))
        for identity in selected:
            if identity not in self.revisions:
                raise TjenticError(
                    f"Missing pinned native marketplace entry for {identity}."
                )
        return selected
