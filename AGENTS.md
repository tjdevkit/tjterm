# Working on TJentic

Start each new context at the checkpoint in the migration plan. Check the named change and its verification results. At each major merge, record what passed, what failed, what was removed and the next authorised action. [Migration plan](plans/migration/PLAN.md)

This file is the shared source of contributor instructions. Keep `CLAUDE.md` as a relative symlink to `AGENTS.md`.

## Verify a contribution

Run the checks from the TJentic checkout.

```sh
uv sync --locked
uv run --locked ruff format --check
uv run --locked ruff check
uv run --locked mypy
uv run --locked pytest
uv build --wheel
uv run --locked tj plugins update --help
```

Verify the installed entry point when a change affects a provider or a user’s terminal.
