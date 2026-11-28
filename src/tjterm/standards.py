from __future__ import annotations

import argparse
import difflib
import re
import stat
import subprocess
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from tjterm.errors import TjtermError

_BEGIN = "<!-- tjentic:standards:start -->"
_END = "<!-- tjentic:standards:end -->"
_AGENTS = "AGENTS.md"
_CONFIG = Path(".tjentic/standards.toml")
_GIT_REDIRECTIONS = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
    }
)


class StandardsError(TjtermError):
    """A project cannot safely synchronize its selected standards."""


@dataclass(frozen=True, slots=True)
class _Source:
    location: str
    revision: str


@dataclass(frozen=True, slots=True)
class _Selection:
    source: _Source
    scope: PurePosixPath
    files: tuple[PurePosixPath, ...]


@dataclass(frozen=True, slots=True)
class _Edit:
    path: Path
    before: bytes | None
    after: bytes


def add_command(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    standards = commands.add_parser("standards", help="manage project standards")
    actions = standards.add_subparsers(dest="standards_action", required=True)
    sync = actions.add_parser("sync", help="preview updates to project instructions")
    sync.add_argument(
        "--apply", action="store_true", help="write the previewed changes"
    )


def _git(
    directory: Path,
    arguments: Sequence[str],
    message: str,
    environment: Mapping[str, str] | None,
) -> bytes:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=directory,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=90,
            check=False,
        )
    except FileNotFoundError as error:
        raise StandardsError("Install Git to synchronize standards.") from error
    except subprocess.TimeoutExpired as error:
        raise StandardsError(f"{message} Git timed out.") from error
    if result.returncode:
        raise StandardsError(message)
    return result.stdout


def _read_utf8(data: bytes, path: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise StandardsError(f"{path} must be UTF-8.") from error


def _relative_path(value: object) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or ":" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise StandardsError("Use worktree-relative paths with forward slashes.")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or ".git" in path.parts:
        raise StandardsError(f"Path leaves the supported worktree scope: {value!r}.")
    return path


def _location(root: Path, value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(ord(c) < 32 for c in value)
    ):
        raise StandardsError(
            "Each standards source must be an HTTPS URL or local path."
        )
    if value.startswith("https://"):
        try:
            parsed = urlsplit(value)
        except ValueError as error:
            raise StandardsError("Invalid HTTPS standards source.") from error
        if (
            parsed.hostname
            and parsed.username is None
            and not (parsed.query or parsed.fragment)
        ):
            return value
        raise StandardsError("Use an HTTPS Git URL without credentials or a query.")
    if "://" in value or "::" in value:
        raise StandardsError("Use an HTTPS Git URL or a local checkout path.")
    location = (root / value).resolve()
    if not location.is_dir():
        raise StandardsError(f"Local standards checkout does not exist: {value!r}.")
    return str(location)


def _manifest(root: Path) -> tuple[_Selection, ...]:
    path = root / _CONFIG
    if path.resolve() != path:
        raise StandardsError("Place .tjentic/standards.toml directly in the worktree.")
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except FileNotFoundError as error:
        raise StandardsError(
            "Select standards in .tjentic/standards.toml first."
        ) from error
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise StandardsError(f"Invalid .tjentic/standards.toml: {error}.") from error
    entries = document.get("standards")
    if document.keys() != {"standards"} or not isinstance(entries, list):
        raise StandardsError(
            "Expected [[standards]] entries in .tjentic/standards.toml."
        )
    selections: list[_Selection] = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.keys() <= {
            "source",
            "revision",
            "scope",
            "files",
        }:
            raise StandardsError(
                "A standards entry accepts source, revision, scope and files."
            )
        revision = entry.get("revision")
        if not isinstance(revision, str) or not re.fullmatch(
            r"[0-9a-fA-F]{40}", revision
        ):
            raise StandardsError("Pin each standards source to a full Git commit ID.")
        file_values = entry.get("files")
        if not isinstance(file_values, list):
            raise StandardsError("List the selected Markdown paths in files.")
        files = tuple(_relative_path(value) for value in file_values)
        if any(file.suffix.lower() != ".md" for file in files):
            raise StandardsError("Every selected standards file must be Markdown.")
        if len(set(files)) != len(files):
            raise StandardsError("List each selected Markdown file once per entry.")
        selections.append(
            _Selection(
                _Source(_location(root, entry.get("source")), revision.lower()),
                _relative_path(entry.get("scope", ".")),
                files,
            )
        )
    return tuple(selections)


def _checkout_root(directory: Path, environment: Mapping[str, str] | None) -> Path:
    result = _git(
        directory,
        ("rev-parse", "--show-toplevel"),
        "Run standards sync inside a Git worktree.",
        environment,
    )
    return Path(
        _read_utf8(result, "Git worktree path").removesuffix("\n").removesuffix("\r")
    ).resolve()


def _read_blob(
    repository: Path,
    revision: str,
    path: PurePosixPath,
    environment: Mapping[str, str] | None,
) -> str:
    label = path.as_posix()
    missing = f"Selected Markdown file {label!r} is missing from {revision}."
    listing = _git(
        repository,
        ("--literal-pathspecs", "ls-tree", "-z", revision, "--", label),
        missing,
        environment,
    )
    if not listing:
        raise StandardsError(missing)
    metadata, _, listed = listing.removesuffix(b"\0").partition(b"\t")
    mode, kind, oid = metadata.split()
    if (
        mode not in {b"100644", b"100755"}
        or kind != b"blob"
        or listed != label.encode()
    ):
        raise StandardsError(f"Select a regular Markdown file: {label!r}.")
    contents = _git(
        repository, ("cat-file", "blob", oid.decode("ascii")), missing, environment
    )
    text = _read_utf8(contents, label)
    if _BEGIN in text or _END in text or "\0" in text:
        raise StandardsError(
            f"{label!r} contains reserved standards markers or binary data."
        )
    if not text.strip():
        raise StandardsError(f"{label!r} contains no standards text.")
    return text


def _fetch_documents(
    source: _Source,
    files: Sequence[PurePosixPath],
    environment: Mapping[str, str] | None,
) -> dict[PurePosixPath, str]:
    with tempfile.TemporaryDirectory(prefix="tjentic-standards-") as temporary:
        repository = Path(temporary)
        _git(
            repository,
            ("init", "--bare", "--template="),
            "Could not create a temporary Git project.",
            environment,
        )
        protocol = "https" if source.location.startswith("https://") else "file"
        failure = (
            f"Could not fetch pinned revision {source.revision} "
            "from the configured Git source."
        )
        _git(
            repository,
            (
                "-c",
                "protocol.allow=never",
                "-c",
                f"protocol.{protocol}.allow=always",
                "-c",
                f"core.hooksPath={repository / 'hooks'}",
                "fetch",
                "--no-tags",
                "--no-recurse-submodules",
                "--depth=1",
                "--",
                source.location,
                source.revision,
            ),
            failure,
            environment,
        )
        resolved = _git(
            repository,
            ("rev-parse", "--verify", "FETCH_HEAD^{commit}"),
            failure,
            environment,
        )
        if resolved.strip().decode("ascii") != source.revision:
            raise StandardsError(failure)
        return {
            file: _read_blob(repository, source.revision, file, environment)
            for file in files
        }


def _managed_span(text: str, path: Path) -> list[re.Match[str]]:
    pattern = rf"(?m)^(?:{re.escape(_BEGIN)}|{re.escape(_END)})(?=\r?$)\r?\n?"
    markers = list(re.finditer(pattern, text))
    if len(markers) == text.count(_BEGIN) + text.count(_END) and (
        not markers
        or (
            len(markers) == 2
            and markers[0].group().rstrip("\r\n") == _BEGIN
            and markers[1].group().rstrip("\r\n") == _END
        )
    ):
        return markers
    raise StandardsError(f"Fix the standards markers in {path} before syncing.")


def _render(before: bytes | None, path: Path, pieces: Sequence[str]) -> bytes:
    existing = _read_utf8(before or b"", str(path))
    markers = _managed_span(existing, path)
    newline = "\r\n" if "\r\n" in existing else "\n"
    block = ""
    if pieces:
        body = "\n\n".join(piece.replace("\r\n", "\n").rstrip("\n") for piece in pieces)
        block = f"{_BEGIN}\n{body}\n{_END}\n".replace("\n", newline)
    if markers:
        rendered = existing[: markers[0].start()] + block + existing[markers[1].end() :]
    elif block and existing and not existing.endswith("\n"):
        rendered = existing + newline + block
    else:
        rendered = existing + block
    return rendered.encode("utf-8")


def _read_agent_file(path: Path) -> bytes | None:
    if path.resolve() != path:
        raise StandardsError(f"Choose an instructions file without symlinks: {path}.")
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _agent_files(root: Path, environment: Mapping[str, str] | None) -> set[Path]:
    listed = _git(
        root,
        (
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            ":(glob)**/AGENTS.md",
        ),
        "Could not select existing project instructions.",
        environment,
    )
    files: set[Path] = set()
    for name in _read_utf8(listed, "Git filenames").split("\0"):
        location = root / name
        if (
            location.name == _AGENTS
            and location.is_file()
            and location.resolve() == location
        ):
            files.add(location)
    return files


def _plan(
    root: Path,
    selections: Sequence[_Selection],
    environment: Mapping[str, str] | None,
) -> list[_Edit]:
    documents: dict[Path, list[str]] = {}
    sources: dict[_Source, list[PurePosixPath]] = {}
    for entry in selections:
        directory = root / entry.scope
        if (
            directory.resolve() != directory
            or not directory.is_dir()
            or _checkout_root(directory, environment) != root
        ):
            raise StandardsError(f"Choose a scope inside this worktree: {entry.scope}.")
        documents.setdefault(directory / _AGENTS, [])
        paths = sources.setdefault(entry.source, [])
        paths.extend(path for path in entry.files if path not in paths)
    resolved = {
        source: _fetch_documents(source, files, environment)
        for source, files in sources.items()
        if files
    }
    for entry in selections:
        pieces = documents[root / entry.scope / _AGENTS]
        pieces.extend(resolved[entry.source][file] for file in entry.files)
    edits: list[_Edit] = []
    for path in sorted(documents.keys() | _agent_files(root, environment)):
        before = _read_agent_file(path)
        if path not in documents and not any(
            marker.encode() in (before or b"") for marker in (_BEGIN, _END)
        ):
            continue
        after = _render(before, path, documents.get(path, ()))
        if after != (before or b""):
            edits.append(_Edit(path, before, after))
    return edits


def _apply(edit: _Edit) -> None:
    if _read_agent_file(edit.path) != edit.before:
        raise StandardsError(f"{edit.path} changed during sync. Run the preview again.")
    mode = stat.S_IMODE(edit.path.stat().st_mode) if edit.before is not None else 0o644
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=edit.path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(edit.after)
        temporary.chmod(mode)
        temporary.replace(edit.path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _preview(edit: _Edit, root: Path) -> None:
    name = edit.path.relative_to(root).as_posix()
    lines = difflib.diff_bytes(
        difflib.unified_diff,
        (edit.before or b"").splitlines(keepends=True),
        edit.after.splitlines(keepends=True),
        b"/dev/null" if edit.before is None else f"a/{name}".encode(),
        f"b/{name}".encode(),
    )
    for line in lines:
        print(line.decode("utf-8"), end="" if line.endswith(b"\n") else "\n")
        if not line.endswith(b"\n"):
            print("\\" + " No newline at end of file")


def run_sync(
    directory: Path,
    *,
    apply: bool,
    environment: Mapping[str, str] | None = None,
) -> None:
    if environment is not None:
        environment = {
            name: value
            for name, value in environment.items()
            if name not in _GIT_REDIRECTIONS
        }
    root = _checkout_root(directory, environment)
    edits = _plan(root, _manifest(root), environment)
    if not edits:
        print("Project standards are up to date.")
        return
    for edit in edits:
        if apply:
            _apply(edit)
            print(f"Updated {edit.path.relative_to(root).as_posix()}")
        else:
            _preview(edit, root)
