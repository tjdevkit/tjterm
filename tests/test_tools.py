from pathlib import Path
from uuid import UUID

import pytest

from tjentic.hosts.host import Host
from tjentic.watch.native import Watch
from tjentic.watch.tools import response

OWNER = UUID("019a0000-0000-7000-8000-000000000001")
OTHER = UUID("019a0000-0000-7000-8000-000000000002")
ISSUE = "https://github.com/paper/widget/issues/7"


@pytest.mark.parametrize("host", [Host.CODEX, Host.CLAUDE])
def test_injected_tool_uses_native_identity_and_ignores_model_supplied_owner(
    host: Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watch = Watch(host, tmp_path, tmp_path / "profile", tmp_path / "listener")

    def register(
        self: Watch, session: UUID, directory: Path, item: str, issue: str
    ) -> str:
        assert self is watch
        assert (session, directory, item, issue) == (OWNER, tmp_path, ISSUE, ISSUE)
        return "Registered"

    monkeypatch.setattr(Watch, "register", register)
    pinned = OWNER if host is Host.CLAUDE else None
    request: dict[str, object] = {
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "watch_pull_request",
            "arguments": {"issue": ISSUE, "thread": str(OTHER)},
            "_meta": {"threadId": str(OWNER if host is Host.CODEX else OTHER)},
        },
    }
    assert response(request, watch, tmp_path, pinned) == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [{"type": "text", "text": "Registered"}],
            "isError": False,
        },
    }


def test_codex_registration_without_a_native_thread_fails_before_binding(
    tmp_path: Path,
) -> None:
    request: dict[str, object] = {
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "watch_pull_request",
            "arguments": {"issue": ISSUE},
        },
    }
    watch = Watch(Host.CODEX, tmp_path, tmp_path, tmp_path / "missing")
    reply = response(request, watch, tmp_path, None)
    assert reply is not None
    assert reply["result"] == {
        "isError": True,
        "content": [
            {"type": "text", "text": "Codex did not supply the calling thread ID."}
        ],
    }
