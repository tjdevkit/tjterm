"""Accept two unrelated projects sharing a disposable native host profile."""

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from verify_native_startup import Capture

type Run = Callable[..., subprocess.CompletedProcess[str]]
type Rules = dict[str, str]
type Request = Callable[[str, Path, dict[str, str], "Capture", Rules, int, Rules], None]


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _commit(repo: Path, env: dict[str, str], run: Run, message: str) -> None:
    run(repo, env, "git", "add", ".")
    run(
        repo,
        env,
        "git",
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--quiet",
        "-m",
        message,
    )


def _project(
    temp: Path,
    env: dict[str, str],
    run: Run,
    *,
    repository: str,
    market: str,
    plugin: str,
    document: str,
    rule: str,
    external_market: bool = False,
) -> tuple[Path, Rules]:
    publisher = temp / (plugin + "-publisher")
    run(temp, env, "git", "init", "--quiet", str(publisher))
    _write(
        publisher / ".claude-plugin/plugin.json",
        json.dumps(
            {"name": plugin, "description": f"Session instructions for {plugin}"}
        ),
    )
    _write(publisher / document, rule)
    _write(
        publisher / "read_rule.py",
        "import json\nimport sys\nfrom pathlib import Path\n"
        "body = (Path(__file__).parent / sys.argv[1]).read_text(encoding='utf-8')\n"
        "print(json.dumps({'hookSpecificOutput': "
        "{'hookEventName': 'SessionStart', 'additionalContext': body}}))\n",
    )
    hook = {
        "type": "command",
        "command": f'python -I "${{CLAUDE_PLUGIN_ROOT}}/read_rule.py" "{document}"',
    }
    group = {"hooks": [hook], **({"matcher": "startup"} if external_market else {})}
    _write(
        publisher / "hooks/hooks.json",
        json.dumps({"hooks": {"SessionStart": [group]}}),
    )
    identity = f"{plugin}@{market}"
    repo = temp / repository
    run(temp, env, "git", "init", "--quiet", str(repo))
    url = publisher.as_uri()
    claude = {"enabledPlugins": {identity: True}}
    codex = f"[plugins.{json.dumps(identity)}]\nenabled = true\n"
    if external_market:
        # Both native marketplace CLIs accept HTTPS Git sources. Git serves this
        # synthetic publisher from disk in the disposable user's profile.
        url = f"https://fixture.example.invalid/{market}.git"
        run(
            temp,
            env,
            "git",
            "config",
            "--global",
            f"url.{publisher.as_uri()}.insteadOf",
            url,
        )
        # The consumer has only native settings. The publisher hosts its catalog.
        _write(
            publisher / ".claude-plugin/marketplace.json",
            json.dumps(
                {
                    "name": market,
                    "owner": {"name": "Fixture"},
                    "plugins": [{"name": plugin, "source": "./"}],
                }
            ),
        )
        _write(
            publisher / ".agents/plugins/marketplace.json",
            json.dumps(
                {
                    "name": market,
                    "interface": {"displayName": market},
                    "plugins": [
                        {
                            "name": plugin,
                            "source": {"source": "local", "path": "./"},
                            "category": "Productivity",
                            "policy": {
                                "installation": "AVAILABLE",
                                "authentication": "ON_INSTALL",
                            },
                        }
                    ],
                }
            ),
        )
        _write(publisher / ".codex-plugin/plugin.json", json.dumps({"name": plugin}))
        claude["extraKnownMarketplaces"] = {
            market: {"source": {"source": "git", "url": url}}
        }
        codex += (
            f"\n[marketplaces.{json.dumps(market)}]\n"
            f'source_type = "git"\nsource = {json.dumps(url)}\n'
        )
    _commit(publisher, env, run, "Publish local startup rule")
    if not external_market:
        pin = run(publisher, env, "git", "rev-parse", "HEAD").stdout.strip()
        _write(
            repo / ".claude-plugin/marketplace.json",
            json.dumps(
                {
                    "name": market,
                    "owner": {"name": "Fixture"},
                    "plugins": [
                        {
                            "name": plugin,
                            "source": {"source": "url", "url": url, "sha": pin},
                        }
                    ],
                }
            ),
        )
    _write(repo / ".claude/settings.json", json.dumps(claude))
    _write(repo / ".codex/config.toml", codex)
    _commit(repo, env, run, "Select the project plugin")
    return repo, {identity: rule}


def verify_project_switches(
    temp: Path,
    env: dict[str, str],
    server: "Capture",
    control: Path,
    previous_rules: Rules,
    run: Run,
    review_hooks: Callable[[Path, dict[str, str]], None],
    check_request: Request,
) -> None:
    first, shipping = _project(
        temp,
        env,
        run,
        repository="shipping-desk",
        market="quayside",
        plugin="berth-log",
        document="dispatch/shift-card.note",
        rule="Record the berth number beside every departure entry.",
    )
    second, personal = _project(
        temp,
        env,
        run,
        repository="letterbox",
        external_market=True,
        market="studio-tools",
        plugin="house-voice",
        document="notes/house-style.txt",
        rule="Address me as Rowan and sign each letter with a sunflower.",
    )
    with (Path(env["CODEX_HOME"]) / "config.toml").open(
        "a", encoding="utf-8"
    ) as config:
        for repo in (first, second):
            config.write(
                f'\n[projects.{json.dumps(str(repo))}]\ntrust_level = "trusted"\n'
            )
    for host in ("codex", "claude"):
        for repo in (first, second):
            run(repo, env, "tj", "plugins", "update", "--host", host)
            if host == "codex":
                review_hooks(repo, env)
        for repo, selected, excluded in (
            (first, shipping, personal),
            (second, personal, shipping),
            (first, shipping, personal),
            (control, {}, shipping | personal),
        ):
            if repo != control:
                run(repo, env, "tj", "plugins", "check", "--host", host)
            check_request(
                host, repo, env, server, selected, 1, excluded | previous_rules
            )
