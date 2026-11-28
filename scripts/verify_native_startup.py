# /// script
# requires-python = ">=3.14"
# dependencies = ["pexpect==4.9.0"]
# ///
"""Verify standards delivery in a linked checkout with disposable native profiles."""

import gzip
import json
import os
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pexpect
from compression import zstd
from native_project_switches import verify_project_switches

PROMPT = "Verify the project's startup standards."


def run(
    cwd: Path, env: dict[str, str], *command: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=90,
    )
    if check and result.returncode:
        raise RuntimeError(f"{command!r} failed: {result.stderr} {result.stdout}")
    return result


def publisher_reference(
    project: Path, env: dict[str, str], host: str, *args: str
) -> str:
    """Read project selection or capture its publishers' startup text."""
    launcher = shutil.which("tj", path=env["PATH"])
    if not launcher:
        raise RuntimeError("Install the candidate wheel in the fixture environment")
    python = Path(launcher).resolve().with_name("python")
    reference = Path(__file__).with_name("capture_publisher_context.py")
    return run(project, env, str(python), str(reference), host, *args).stdout


def occurrences(value: object, text: str) -> int:
    if isinstance(value, str):
        return value.count(text)
    if isinstance(value, list):
        return sum(occurrences(item, text) for item in value)
    if isinstance(value, dict):
        return sum(occurrences(item, text) for item in value.values())
    return 0


class Capture(ThreadingHTTPServer):
    """Capture only model requests and stop each CLI before it can take any action."""

    def __init__(self) -> None:
        self.requests: list[object] = []
        super().__init__(("127.0.0.1", 0), Handler)


class Handler(BaseHTTPRequestHandler):
    server: Capture

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        match self.headers.get("Content-Encoding"):
            case "gzip":
                body = gzip.decompress(body)
            case "zstd":
                body = zstd.decompress(body)
        if self.path.partition("?")[0].endswith(("/messages", "/responses")):
            self.server.requests.append(json.loads(body))
        self.send_response(400)
        self.end_headers()
        self.wfile.write(b'{"error":{"message":"end of local test"}}')


def check_request(
    host: str,
    project: Path,
    env: dict[str, str],
    server: Capture,
    rules: dict[str, str],
    expected: int,
    excluded: dict[str, str] | None = None,
) -> None:
    server.requests.clear()
    if host == "codex":
        command = ["codex", "exec", "--json", PROMPT]
    else:
        command = [
            "claude",
            "-p",
            "--model",
            "claude-sonnet-4-5",
            "--max-turns",
            "1",
            PROMPT,
        ]
    response = run(project, env, *command, check=False)
    packets = [packet for packet in server.requests if occurrences(packet, PROMPT)]
    if not packets:
        raise RuntimeError(
            f"{host} sent no user request from {project.name}: {response.stderr}"
        )
    counts = {name: occurrences(packets[0], rule) for name, rule in rules.items()}
    if any(count != expected for count in counts.values()):
        raise AssertionError(
            f"{project.name} {host}: expected {expected} of each rule, got {counts}"
        )
    for name, rule in (excluded or {}).items():
        if occurrences(packets[0], rule):
            raise AssertionError(f"{project.name} {host}: unexpected rule from {name}")
    print(
        f"{host} in {project.name}: {len(rules)} rules with {expected} copies each, "
        f"{len(excluded or {})} other rules absent"
    )


def review_fixture_hooks(project: Path, env: dict[str, str]) -> None:
    # The fixture owns this profile and installed only the project's pinned hooks.
    with pexpect.spawn(
        "codex",
        ["--no-alt-screen", "--no-daemon"],
        cwd=str(project),
        env=env,
        encoding="utf-8",
        timeout=30,
        dimensions=(50, 150),
    ) as terminal:
        terminal.expect_exact("Review hooks")
        terminal.send("2")
        terminal.send("\r")
        terminal.expect_exact("Trusting hooks")
        terminal.expect_exact("Ask Codex to do anything")


def verify(root: Path, temp: Path, server: Capture, search_path: str) -> None:
    source, project, control = temp / "primary", temp / "linked", temp / "control"
    home = temp / "user"
    codex_home = home / ".codex"
    codex_home.mkdir(parents=True)
    url = f"http://127.0.0.1:{server.server_port}"
    env = {
        "HOME": str(home),
        "TMPDIR": str(temp),
        "PATH": search_path,
        "LANG": "C.UTF-8",
        "TERM": "xterm-256color",
        "CODEX_HOME": str(codex_home),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "ANTHROPIC_BASE_URL": url,
        "ANTHROPIC_API_KEY": "fixture-only",
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "API_RETRY_MAX": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    # Copy the committed candidate into a real registered sibling worktree.
    revision = run(root, env, "git", "rev-parse", "HEAD").stdout.strip()
    run(temp, env, "git", "clone", "--quiet", "--no-hardlinks", str(root), str(source))
    run(
        source,
        env,
        "git",
        "worktree",
        "add",
        "--quiet",
        "--detach",
        str(project),
        revision,
    )
    run(temp, env, "git", "init", "--quiet", str(control))
    (codex_home / "config.toml").write_text(
        'model = "gpt-5-codex"\nmodel_provider = "fixture"\n'
        '[model_providers.fixture]\nname = "fixture"\nwire_api = "responses"\n'
        f'base_url = "{url}/v1"\n'
        "requires_openai_auth = false\nsupports_websockets = false\n"
        "request_max_retries = 0\nstream_max_retries = 0\n"
        + "".join(
            f'[projects.{json.dumps(str(p))}]\ntrust_level = "trusted"\n'
            for p in (source, project, control)
        ),
        encoding="utf-8",
    )
    project_rules = {}
    for host in ("codex", "claude"):
        selected = json.loads(publisher_reference(project, env, host, "--selected"))
        if not selected:
            continue
        command = ("tj", "plugins", "check", "--host", host)
        if run(project, env, *command, check=False).returncode == 0:
            raise AssertionError(f"{host} accepted missing standards")
        run(project, env, "tj", "plugins", "update", "--host", host)
        reviewed = False
        if host == "codex":
            status = run(project, env, *command, check=False)
            if status.returncode:
                if "untrusted" not in status.stderr:
                    raise RuntimeError(status.stderr)
                review_fixture_hooks(project, env)
                reviewed = True
        run(project, env, *command)
        rules = json.loads(publisher_reference(project, env, host))
        if host == "codex" and rules and not reviewed:
            raise AssertionError("Codex accepted unreviewed startup hooks")
        project_rules.update(rules)
        profile_record = (
            codex_home / "config.toml"
            if host == "codex"
            else home / ".claude/plugins/installed_plugins.json"
        )
        before = profile_record.read_bytes()
        run(project, env, "tj", "plugins", "update", "--host", host)
        if profile_record.read_bytes() != before:
            raise AssertionError(f"{host} changed its configuration on repeated update")
        check_request(host, project, env, server, rules, 1)
        check_request(host, control, env, server, rules, 0)
    verify_project_switches(
        temp,
        env,
        server,
        control,
        project_rules,
        run,
        review_fixture_hooks,
        check_request,
    )


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    with (
        tempfile.TemporaryDirectory(prefix="tj-startup-") as directory,
        Capture() as server,
    ):
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            verify(root, Path(directory), server, os.environ["PATH"])
        finally:
            server.shutdown()
            worker.join()


if __name__ == "__main__":
    main()
