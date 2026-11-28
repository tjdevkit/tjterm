"""Obtain the reference text from selected publishers in the disposable CI profile."""

import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

from tjentic.hosts.claude import plugins as claude
from tjentic.hosts.codex import plugins as codex
from tjentic.hosts.codex.server import codex_server
from tjentic.hosts.host import Host
from tjentic.plugins.worktree import checked_selection


def plugin_root(source: Path) -> Path:
    for parent in source.parents:
        if any(
            (parent / name).is_file()
            for name in (
                ".codex-plugin/plugin.json",
                ".claude-plugin/plugin.json",
                ".cursor-plugin/plugin.json",
                "plugin.json",
            )
        ):
            return parent
    raise ValueError(f"No publisher manifest above the registered hook {source}")


def commands(
    host: Host, project: Path, enabled: set[str], env: dict[str, str]
) -> Iterator[tuple[str, Path, str, str | list[str]]]:
    if host is Host.CODEX:
        with codex_server(project, env) as server:
            groups = server.request("hooks/list", {"cwds": [str(project)]})["data"]
        for group in groups:
            for hook in group["hooks"]:
                if (
                    hook.get("pluginId") in enabled
                    and hook["eventName"] == "sessionStart"
                ):
                    assert (
                        hook["trustStatus"] in ("trusted", "managed")
                        and hook["enabled"]
                    )
                    root = plugin_root(Path(hook["sourcePath"]))
                    yield (
                        hook["pluginId"],
                        root,
                        hook.get("matcher") or "",
                        hook["command"],
                    )
        return
    for identity in sorted(enabled):
        install = claude.installation(project, identity, env)
        assert install is not None
        # Native listings display Claude's exec-form argv as one string.
        # Read the installed manifest to preserve its argument boundaries.
        for _, matcher, handler in claude.startup_handlers(install):
            command = handler["command"]
            if "args" in handler:
                command = [
                    part.replace("${CLAUDE_PLUGIN_ROOT}", str(install.root))
                    for part in (command, *handler["args"])
                ]
            yield identity, install.root, matcher, command


def main() -> None:
    host = Host(sys.argv[1])
    project = Path.cwd()
    env = dict(os.environ)
    selector = codex.selected if host is Host.CODEX else claude.selected
    _, selected = checked_selection(project, host, selector, env)
    if "--selected" in sys.argv:
        print(json.dumps(selected))
        return
    event = {
        "hook_event_name": "SessionStart",
        "source": "startup",
        "cwd": str(project),
        "session_id": "00000000-0000-4000-b000-000000000001",
    }
    contexts = {}
    for index, (plugin, root, matcher, command) in enumerate(
        commands(host, project, set(selected), env)
    ):
        if matcher not in ("", "*") and re.search(matcher, "startup") is None:
            continue
        result = subprocess.run(
            command,
            shell=isinstance(command, str),
            cwd=project,
            env=env | {"PLUGIN_ROOT": str(root), "CLAUDE_PLUGIN_ROOT": str(root)},
            input=json.dumps(event) + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            timeout=30,
        )
        text = result.stdout.strip()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            context = text
        else:
            context = payload.get("hookSpecificOutput", {}).get("additionalContext", "")
        if context.strip():
            contexts[f"{host}/{plugin}/{index}"] = context.strip()
    print(json.dumps(contexts))


if __name__ == "__main__":
    main()
