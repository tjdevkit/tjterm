import asyncio
import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

from tjentic.errors import TjenticError
from tjentic.sessions.hosts import Host


class Model(StrEnum):
    OPUS = "opus"
    SONNET = "sonnet"


@dataclass(frozen=True, slots=True)
class SavedSession:
    directory: Path
    transcript: Path


HANDOFF_HINT = (
    "When the user asks to hand off a task, run tj handoff 'TASK'. "
    "For a tracked item run tj handoff ITEM --next ACTION. "
    "Continue the user's other work here."
)


def arguments(session: UUID, *, resume: bool) -> list[str]:
    return [
        "--resume" if resume else "--session-id",
        str(session),
        "--append-system-prompt",
        HANDOFF_HINT,
    ]


def launch(
    command: Sequence[str], session_id: UUID | None, environment: Mapping[str, str]
) -> int:
    identity = session_id or uuid4()
    result = subprocess.run(
        [*command, *arguments(identity, resume=session_id is not None)],
        env={
            **environment,
            "TJ_HOST": Host.CLAUDE,
            Host.CLAUDE.session_variable: str(identity),
        },
        check=False,
    )
    return result.returncode if result.returncode >= 0 else 128 - result.returncode


def _projects(environment: Mapping[str, str]) -> Path:
    config = Path(environment.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return config.expanduser().resolve() / "projects"


def transcript(directory: Path, identity: UUID, environment: Mapping[str, str]) -> Path:
    project = re.sub(r"[^a-zA-Z0-9-]", "-", str(directory))
    return _projects(environment) / project / f"{identity}.jsonl"


def saved_session(identity: UUID, environment: Mapping[str, str]) -> SavedSession:
    candidates = list(_projects(environment).glob(f"*/{identity}.jsonl"))
    if len(candidates) == 1:
        history = candidates[0]
        with history.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    record: object = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict) or record.get("sessionId") != str(
                    identity
                ):
                    continue
                cwd = record.get("cwd")
                if isinstance(cwd, str) and Path(cwd).is_absolute():
                    return SavedSession(Path(cwd).resolve(), history)
    raise TjenticError(f"No unique saved Claude session matches {identity}.")


async def assess(
    command: Sequence[str],
    directory: Path,
    prompt: str,
    schema: object,
    settings: Model,
    *,
    effort: str,
    environment: Mapping[str, str],
) -> str:
    process = await asyncio.create_subprocess_exec(
        *command,
        "-p",
        "--model",
        settings.value,
        "--effort",
        effort,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema),
        "--tools",
        "",
        "--no-session-persistence",
        "--max-turns",
        "1",
        cwd=directory,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={key: value for key, value in environment.items() if key != "CLAUDECODE"},
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(prompt.encode()), timeout=90
        )
    except TimeoutError as error:
        raise TjenticError("Claude lane assessment timed out.") from error
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode:
        detail = stderr.decode(errors="replace")[-500:].strip()
        raise TjenticError(f"Claude lane assessment failed. {detail}")
    result: object = json.loads(stdout)
    if not isinstance(result, dict) or not isinstance(
        result.get("structured_output"), dict
    ):
        raise TjenticError("Claude returned an invalid lane assessment.")
    return json.dumps(result["structured_output"])
