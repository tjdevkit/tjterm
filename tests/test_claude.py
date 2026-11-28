import json
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest

from tjentic.errors import TjenticError
from tjentic.hosts.host import Host
from tjentic.sessions.claude import launch


@pytest.mark.parametrize(
    "session_id", [None, UUID("019a0000-0000-7000-8000-000000000001")]
)
def test_launch_keeps_the_project_and_forwards_native_resume(
    session_id: UUID | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    script = (
        "import json, os, sys; print(json.dumps([os.getcwd(), sys.argv[1:], "
        "os.getenv('CLAUDE_CODE_SESSION_ID'), os.getenv('TJ_HOST')])); "
        "sys.exit(23)"
    )
    assert launch([sys.executable, "-c", script], session_id, {}) == 23
    directory, arguments, caller, host = json.loads(capfd.readouterr().out)
    assert directory == str(tmp_path)
    assert host == Host.CLAUDE
    assert UUID(caller) == (session_id or UUID(arguments[1]))
    assert arguments[:2] == ["--resume" if session_id else "--session-id", caller]


@pytest.mark.parametrize(
    "session_id", [None, UUID("019a0000-0000-7000-8000-000000000001")]
)
def test_launch_refuses_incomplete_project_standards_before_opening_claude(
    session_id: UUID | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".tjentic").mkdir()
    (tmp_path / ".tjentic" / "standards.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(TjenticError, match=r"standards\.toml"):
        launch(["must-not-run"], session_id, {})
