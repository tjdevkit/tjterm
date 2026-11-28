import json
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def editorial(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    subprocess.run(
        ["git", "init", "-q", "--initial-branch=trunk", str(project)], check=True
    )
    for folder in (".claude-plugin", ".claude", ".codex"):
        (project / folder).mkdir()
    pins = {"captions": "1" * 40, "voice": "2" * 40, "archived": "3" * 40}
    plugins = [
        {
            "name": name,
            "source": {
                "source": "url",
                "url": f"https://example.org/{name}",
                "sha": pin,
            },
        }
        for name, pin in pins.items()
    ]
    (project / ".claude-plugin/marketplace.json").write_text(
        json.dumps({"name": "editorial", "plugins": plugins}), encoding="utf-8"
    )
    selected = {f"{name}@editorial": True for name in ("captions", "voice")}
    (project / ".claude/settings.json").write_text(
        json.dumps({"enabledPlugins": selected}), encoding="utf-8"
    )
    (project / ".codex/config.toml").write_text(
        "".join(f'[plugins."{key}"]\nenabled = true\n' for key in selected),
        encoding="utf-8",
    )
    return project
