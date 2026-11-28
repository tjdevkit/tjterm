"""Exercise the installed TJ launchers in disposable native terminals."""

import json
from pathlib import Path
from threading import Event

import pexpect


def _terminal(host: str, project: Path, env: dict[str, str]) -> pexpect.spawn:
    return pexpect.spawn(
        "tj",
        [host],
        cwd=str(project),
        env=env,
        encoding="utf-8",
        timeout=30,
        dimensions=(50, 150),
    )


def _onboard_claude(project: Path, env: dict[str, str]) -> None:
    # Approve the loopback key and fixture directory in this disposable profile.
    profile = Path(env["CLAUDE_CONFIG_DIR"]) / ".claude.json"
    settings = (
        json.loads(profile.read_text(encoding="utf-8")) if profile.exists() else {}
    )
    settings.update(
        hasCompletedOnboarding=True,
        theme="dark",
        customApiKeyResponses={"approved": [env["ANTHROPIC_API_KEY"][-20:]]},
    )
    # TJ and Claude both exit after capture. Register all the fixture's Git roots
    # together so concurrent native config saves retain the next checkout's trust.
    projects = settings.setdefault("projects", {})
    for repo in project.parent.iterdir():
        if (repo / ".git").exists():
            projects.setdefault(str(repo), {})["hasTrustDialogAccepted"] = True
    profile.parent.mkdir(exist_ok=True, parents=True)
    profile.write_text(json.dumps(settings), encoding="utf-8")


def submit(
    host: str, project: Path, env: dict[str, str], prompt: str, received: Event
) -> None:
    if host == "claude":
        _onboard_claude(project, env)
    with _terminal(host, project, env) as terminal:
        terminal.expect_exact(
            "Ask Codex to do anything"
            if host == "codex"
            else "\N{HEAVY RIGHT-POINTING ANGLE QUOTATION MARK ORNAMENT}"
        )
        terminal.send(prompt)
        terminal.expect_exact(prompt.split()[-1])
        terminal.send("\r")
        if not received.wait(timeout=30):
            raise AssertionError(f"tj {host} sent no user request from {project}")
        terminal.sendcontrol("d")
        try:
            terminal.expect(pexpect.EOF, timeout=2)
        except pexpect.TIMEOUT:
            terminal.close(force=True)


def expect_refusal(host: str, project: Path, env: dict[str, str], error: str) -> None:
    with _terminal(host, project, env) as terminal:
        terminal.expect_exact(error.strip())
        terminal.expect(pexpect.EOF)
        terminal.close()
        if not terminal.exitstatus:
            raise AssertionError(f"tj {host} accepted the blocked launch in {project}")
