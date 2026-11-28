import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tjentic.errors import TjenticError
from tjentic.sessions.codex import Codex, Model, Session, receipt


@pytest.mark.allow_hosts(["127.0.0.1"])
def test_create_resolves_current_release_in_each_family(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Codex(MagicMock(), MagicMock(), {})
    families = [
        (Model.SOL, "gpt-6.10-sol"),
        (Model.ASTRA, "gpt-6.10-astra"),
        (Model.TERRA, "gpt-6.2-terra"),
        (Model.LUNA, "gpt-6-luna"),
    ]
    root = tmp_path.resolve()
    thread = {
        "id": "019a0000-0000-7000-8000-000000000001",
        "cwd": str(root),
        "updatedAt": 1,
    }
    request = AsyncMock(
        side_effect=[
            {
                "data": [{"model": "gpt-6.9-sol"}, {"model": "gpt-6.1-terra"}],
                "nextCursor": "next",
            },
            {
                "data": [
                    {"model": "gpt-6.10-sol"},
                    {"model": "gpt-6.10-astra"},
                    {"model": "gpt-6.2-terra"},
                    {"model": "gpt-6-luna"},
                    {"model": "gpt-9"},
                ],
            },
            *(
                {"model": slug, "reasoningEffort": "high", "thread": thread}
                for _, slug in families
            ),
        ],
    )
    monkeypatch.setattr(client, "request", request)

    async def lookup() -> None:
        for family, slug in families:
            opened = await client.create(root, family, effort="high")
            assert opened.settings.model == slug

    asyncio.run(lookup())
    calls = [call.args for call in request.await_args_list]
    assert [method for method, _ in calls] == ["model/list"] * 2 + ["thread/start"] * 4
    assert [params["model"] for _, params in calls[2:]] == [
        slug for _, slug in families
    ]


@pytest.mark.parametrize("value", [10**40, True, "yesterday"])
def test_invalid_history_dates_are_reported(tmp_path: Path, value: object) -> None:
    metadata = {
        "id": "019a0000-0000-7000-8000-000000000001",
        "cwd": str(tmp_path),
        "updatedAt": value,
    }
    with pytest.raises(TjenticError, match="timestamp"):
        Session.from_thread(metadata)


def test_delivery_requires_matching_user_turn_and_completed_reply() -> None:
    user = {"type": "userMessage", "content": [{"type": "text", "text": "check A"}]}
    reply = {"type": "agentMessage", "text": "check B"}
    items = [user, reply]
    assert receipt([{"status": "completed", "items": items}], "check A") == "check B"
    assert receipt([{"status": "completed", "items": items}], "check B") is None
    with pytest.raises(TjenticError, match="interrupted"):
        receipt([{"status": "interrupted", "items": items}], "check A")
