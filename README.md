# TJentic

TJentic opens Claude Code or Codex in your terminal. This preview also finds saved Codex conversations and can hand a task to another Codex session.

## Open a session

From your project directory, run `tj claude` for Claude Code or `tj codex` for Codex. Each launcher uses that host's executable on your PATH, account, and settings. To reopen a Claude conversation, run `tj claude --resume SESSION_ID` in its project. Use `tj codex --resume SESSION_ID` to open a saved Codex conversation in its recorded directory with TJ's handoff instructions.

## Recover a Codex session

Install the preview with `uv tool install .` from a checkout, or run `uv run --locked tj --help` to try the checkout's environment. The command uses the developer's existing Codex CLI, account and native settings. Python 3.12 or newer is required.

```sh
tj recover
tj recover "the phrase you remember"
tj recover --all "the phrase you remember"
tj recover --resume YOUR_SESSION_UUID
```

From a Git checkout, the default listing includes sessions from its registered worktrees and their subdirectories. Outside Git it uses the current directory. Use `--all` to search other projects, `--limit N` for more results, and `--json` for scripts. Each result identifies its provider, session ID and recorded directory. The list and search ask Codex for non-archived interactive sessions visible to the current account using its native history service. Codex manages its own history indexes. [Codex thread listing and search](https://github.com/openai/codex/blob/3d86e43b075adeba2193ec5c22ac92f55afd82e6/codex-rs/app-server/README.md#local-history-by-account)

A disconnected worktree can still appear with its recorded path. If a different repository takes over that path, its sessions can be found with `--all`.

An explicit resume can be run from any directory. TJ resolves the exact UUID and checks that its original directory still exists before handing the terminal to `codex resume --no-daemon --cd DIRECTORY UUID`. The independent Codex client acquires the native exclusive writer lock. If another process owns the session or its directory has vanished, finish or recover that situation before retrying. No personal plugin, Git remote or TJ account is required. [Codex writer lock](https://github.com/openai/codex/blob/3d86e43b075adeba2193ec5c22ac92f55afd82e6/codex-rs/rollout/src/writer_lock.rs#L45-L85)

This preview targets Codex 0.172.0 on Arch Linux. Claude recovery and native Windows host acceptance are pending.

[Contributor instructions](AGENTS.md)

## Run a task through a local check with Codex

From the target Git worktree, give TJ a unique item name, the task you authorize,
and the project's check command. Put the command and its arguments last.

```sh
tj workflow run widgets --task "Handle an empty widget list" --check python -m pytest
tj workflow status widgets
tj workflow resume widgets
```

TJ runs a read-only assessment on the configured L6 model. It chooses L1 for
critical design, L2 for high-stakes judgment, L3 for investigation, L4 for
specified implementation, L5 for mechanical work, or L6 for clerical work.
The assessment saves the selected model, effort and reason before launch.
Configure each rung from L1 to L6 under `[codex.L1]` through `[codex.L6]` in
`~/.config/tjentic/lanes.toml` (or under `$XDG_CONFIG_HOME` or `%APPDATA%`).
Each section sets `effort` and can set a preferred `tier` (`astra`, `sol`, `terra`
or `luna`). TJ reads Codex's current model catalog before each new selection.
Within the requested family it chooses the newest model. Existing items retry
with their recorded model and effort.

To start at verification in a saved conversation, bind that conversation from
its Git worktree.

```sh
tj workflow start widgets --thread SESSION_UUID --check python -m pytest
```

The check runs at the worktree root. TJ records its exit code, revision, Git
status and the last 16 KiB of output. A failure asks the bound conversation to
repair the work and request another check. A pass leaves the item ready for
local review. Use `status` for the bound session, phase, latest result and hold
reason. The state lives under `$XDG_STATE_HOME/tjentic` (normally
`~/.local/state/tjentic` on Linux).
`tj` also reads a database or lane configuration from the TJ Term preview when
the corresponding TJentic file is absent.

If the native terminal owns the target thread, finish that session and use
`resume` after it releases the writer lock. An interrupted delivery checks
native history for the original result. An uncertain receipt requires
inspection. If TJ stopped during the check itself, inspect its effects before
using `tj workflow retry-check widgets`. To pause between foreground runs use
`tj workflow hold widgets "waiting for input"`. Use `tj workflow release widgets`
after resolving the reason.

## Hand off a task

In a Kitty tab in your Git worktree, run:

```sh
tj handoff "Review the parser change"
```

The receiving Claude Code or Codex conversation opens in another tab in the
same window, in the item's worktree. TJ sends the assignment as the first prompt
and transfers ownership when the native session records it.
You can keep using your original tab while the recipient starts.

For an existing item, use `tj handoff ITEM --next "Review the parser change"`.
From the shell use `--host claude` to select Claude Code. See the recipient and
lane with `tj workflow status ITEM` in the item's worktree. If the recipient
closes before receiving the task, use `tj handoff --retry TRANSFER_ID`.

## Synchronize project standards

Add `.tjentic/standards.toml` to your Git worktree and select the Markdown files your team wants in its native agent instructions. Pin each source to a full Git commit. Sources can be HTTPS Git URLs or local checkout paths.

```toml
[[standards]]
source = "https://github.com/tjentic/python-standards.git"
revision = "8a3bf5a435227cfbc681d20e4a0286e7d8d30887"
files = ["standards/convention/enums.md"]

[[standards]]
source = "https://github.com/tjentic/rust-standards.git"
revision = "05374eac81ce4bfffbe89fb71deaf12fcfdde2c3"
scope = "crates"
files = ["standards/convention/domain-design.md"]
```

Omit `scope` for the worktree root. Give each scoped entry an existing directory in the worktree. Select a short set of rules so both clients have room for the project's own instructions.

```sh
tj plugins update
```

The command updates a marked section in each selected `AGENTS.md`. Add `--dry-run` to see a unified diff before writing. Review and commit the file and manifest together so teammates can use the text with their normal agent commands. Update a pin or file list and run sync again to refresh the section. Removing an entry clears its old section in tracked or unignored instruction files. An explicit empty `files` list can clear an ignored target while its scope is still selected.

Codex loads the section according to its usual scope and gives `AGENTS.override.md` precedence. Claude Code can use the same file through native discovery, an existing `CLAUDE.md` → `AGENTS.md` symlink, or a local `@AGENTS.md` import in a separately authored `CLAUDE.md`. Start in the relevant subtree when you need its rules on the first turn.
