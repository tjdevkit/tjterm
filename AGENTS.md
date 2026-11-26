# Working on TJ Term

Start each new context at the checkpoint in the migration plan. Check the named change and its verification results. At each major merge, record what passed, what failed, what was removed and the next authorised action. [Migration plan](plans/migration/PLAN.md)

This file is the shared source of contributor instructions. Keep `CLAUDE.md` as a relative symlink to `AGENTS.md`.

## Python

Use Python 3.12 or newer and manage dependencies with uv. Commit `uv.lock` when changing dependencies. Follow the pinned contributor standards for authored Python, including host adapters and scripts. The scaffold adopts their Ruff, mypy and pytest settings. [Python standards](https://github.com/tj-agents/python/tree/cd143949fb027ec30ed0267dfd37a76c5cc17910/.agents/python)

Validate the scaffold with these commands.

```sh
uv sync --locked
uv run --locked ruff check pyproject.toml
```

When the first Python source arrives, add a test of its behaviour and enable these checks locally and in CI.

```sh
uv run --locked ruff format --check
uv run --locked ruff check
uv run --locked mypy
uv run --locked pytest
```

Include the pinned house source checker with `--root .` in that change. Replace this temporary development pin with the independent Python standards release before retiring its existing publisher. Verify the installed entry point when a change affects a provider or a user's terminal. [House checker](https://github.com/tj-agents/python/blob/cd143949fb027ec30ed0267dfd37a76c5cc17910/.agents/python/utility/check/scripts/check_python.py)
