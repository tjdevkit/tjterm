"""Inspect the native host's startup registration without running its hooks."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Hashable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from tjentic.errors import TjenticError
from tjentic.plugins.tree import PackageTree, TreeError


class StartupError(TjenticError):
    """The native host cannot verify a selected standards registration."""


def as_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise StartupError("Expected a JSON object from the native host.")
    return value


def as_rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise StartupError("Expected a JSON list from the native host.")
    return [as_object(row) for row in value]


def read_object(path: Path) -> dict[str, object]:
    try:
        return as_object(json.loads(path.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError) as error:
        raise StartupError(f"Cannot read {path}: {error}") from error


@dataclass(frozen=True, slots=True)
class Installation:
    root: Path
    revision: str

    def hook_handlers(
        self, manifest: Path, declarations: object, *, merge_default: bool
    ) -> Iterator[tuple[Path, str, dict[str, object]]]:
        """Read every declared SessionStart handler from the installed package."""
        conventional = self.root / "hooks/hooks.json"
        sources = declarations if isinstance(declarations, list) else [declarations]
        if (merge_default or declarations is None) and conventional.is_file():
            yield from _handlers(conventional, read_object(conventional).get("hooks"))
        for source in sources:
            if source is None:
                continue
            if isinstance(source, str):
                path = (self.root / source).resolve()
                if not path.is_relative_to(self.root.resolve()):
                    raise StartupError(f"Hook source {source} escapes {self.root}.")
                yield from _handlers(path, read_object(path).get("hooks"))
            else:
                # Claude inline declarations are event maps. Codex uses a hooks file.
                events = source if merge_default else as_object(source).get("hooks")
                yield from _handlers(manifest, events)

    def check_alias(
        self,
        identity: str,
        other: str,
        installation: Installation | None,
        *,
        tree: PackageTree | None = None,
        native_metadata: Callable[[Path, Path], bool] | None = None,
    ) -> None:
        if installation is None or installation.revision != self.revision:
            return
        if tree:
            # Siblings can share a hook command and a Git commit.
            try:
                tree.verify(self.root, native_metadata=native_metadata)
                tree.verify(installation.root, native_metadata=native_metadata)
            except TreeError:
                return
        raise StartupError(
            f"{identity} and {other} both register the same standards revision. "
            f"Disable the duplicate plugin {other} in this checkout."
        )


def native_matcher(value: object) -> str:
    if value is None or value == "*":
        return ""
    if isinstance(value, str):
        return value
    raise StartupError("Expected a native hook matcher string.")


def _handlers(
    path: Path, events: object
) -> Iterator[tuple[Path, str, dict[str, object]]]:
    for group in as_rows(as_object(events).get("SessionStart", [])):
        matcher = native_matcher(group.get("matcher"))
        for handler in as_rows(group.get("hooks")):
            command = handler.get("command")
            if (
                handler.get("type") != "command"
                or handler.get("async", False)
                or not isinstance(command, str)
                or not command.strip()
            ):
                raise StartupError(
                    f"Expected a synchronous SessionStart command in {path}."
                )
            yield path, matcher, handler


def require_commands[T: Hashable](
    identity: str, expected: Iterable[T], actual: Iterable[T]
) -> None:
    if Counter(expected) != Counter(actual):
        raise StartupError(
            f"Incomplete native SessionStart registration for {identity}."
        )


def require_revision(
    identity: str, install: Installation | None, pin: str | None
) -> Installation:
    if (
        install is None
        or not re.fullmatch(r"[0-9a-fA-F]{40}", install.revision)
        or (pin is not None and install.revision != pin)
    ):
        actual = install.revision if install else "missing"
        raise StartupError(
            f"{identity} needs {pin or 'an installed Git revision'}, found {actual}. "
            "Run tj plugins update here."
        )
    return install
