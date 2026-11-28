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
uv build
uv run --locked tj plugins update --help
```

Verify the installed entry point when a change affects a provider or a user’s terminal.

When implementing a generic requirement, treat named examples as test cases. During review, trace each restriction to the requirement or an upstream contract and exercise a valid case the request never named.

For standards discovery, open two unrelated repositories with different plugin selections using the same native profile. Check the first request from the installed command against each repository’s selected publisher content.
