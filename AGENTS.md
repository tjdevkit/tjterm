# Working on TJentic

Start each new context at the checkpoint in the migration plan. Check the named change and its verification results. At each major merge, record what passed, what failed, what was removed and the next authorised action. [Migration plan](plans/migration/PLAN.md)

This file is the shared source of contributor instructions. Keep `CLAUDE.md` as a relative symlink to `AGENTS.md`.

## Python

Use Python 3.12 or newer and manage dependencies with uv. Commit `uv.lock` when changing dependencies. Follow the pinned contributor standards for authored Python, including host adapters and scripts. The project uses their Ruff, mypy and pytest settings. [Python standards](https://github.com/tjentic/python-standards/tree/8a3bf5a435227cfbc681d20e4a0286e7d8d30887/standards)

Validate authored Python with these commands.

```sh
uv sync --locked
uv run --locked ruff format --check
uv run --locked ruff check
uv run --locked mypy
uv run --locked pytest
uv build --wheel
uv run --locked tj plugins update --help
uv tool run --isolated --python 3.12 \
  --from git+https://github.com/tjentic/python-standards.git@8a3bf5a435227cfbc681d20e4a0286e7d8d30887 \
  check-python-standards --root .
```

Verify the installed entry point when a change affects a provider or a user's terminal. [House checker](https://github.com/tjentic/python-standards/blob/8a3bf5a435227cfbc681d20e4a0286e7d8d30887/standards/tool/check-python-standards.md)
