# TJentic

TJentic opens Claude Code or Codex in your terminal. This preview also finds saved Codex conversations and can hand a task to another Codex session.

## Open a session

From your project directory, run `tj claude` for Claude Code or `tj codex` for Codex. Each launcher uses that host's executable on your PATH, account, and settings. To reopen a Claude conversation, run `tj claude --resume SESSION_ID` in its project. Use `tj codex --resume SESSION_ID` to open a saved Codex conversation in its recorded directory with TJ's handoff instructions.

## Wait for a linked pull request

In `tj codex` or `tj claude`, ask the agent to finish a GitHub issue when its linked PR merges. Include the issue URL. When the PR is the sole remaining condition, the injected `watch_pull_request` tool reads the issue's closing links using your authenticated `gh` CLI and verifies the PR head against the checkout. It records the issue URL as the item name unless the session supplies an existing TJ item name.

The Rust listener follows the PR after the terminal closes. Starting either managed launcher recovers outstanding waits. A merge of the accepted revision records one local closeout. A changed revision, changed link, or closed-unmerged PR leaves the item open and sends the owner a review event. The owner can explicitly register a replacement PR for the same issue once its head matches the checkout. Earlier observations and notices stay tied to the old registration. Check the result in the item's worktree with `tj workflow status ITEM`. External issue updates and terminal cleanup are separate operations.

Before sending a review event, the listener rechecks the item’s owner, hold, accepted revision and latest local check. It records the claim and releases SQLite before contacting the host. A hold or handoff can take effect while that send is in flight. In `tj workflow status ITEM`, a `queued` receipt means Codex accepted the message and an `emitted` receipt means TJ flushed the event to Claude’s monitor stream. The background listener keeps polling other waits while Codex acknowledges a send. TJ retries a definite failure to start the send. If the host’s acknowledgement is lost, the receipt is `uncertain`. A listener interrupted during submission can leave a `dispatching` receipt. Inspect the recorded session before resending either one.

Claude’s session plugin runs `tj watch` to receive events. On versions without plugin monitors the tool can arm a native background Bash subscription. Codex uses its native queue addressed to the recorded thread. Routine merges and unchanged CI use zero model requests. Source installations compile the bundled listener with Cargo and a C compiler. The wheel includes the listener.

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

## Set up contributor plugins

Each Git checkout selects its own contributor plugins. From the checkout you plan to work in, install its selection for your host.

```sh
tj plugins update --host codex
tj plugins check --host codex
```

For Claude Code, use `--host claude`. Use `--dry-run` with `update` to see the selected plugin IDs and any pinned revisions. In Codex, review the plugin commands through the native hook trust prompt or `/hooks`. Start a new session or resume the intended one after installation. A standards publisher supplies its session instructions through its native `SessionStart` hooks.

### Project selection

For a repository with its own marketplace, declare each plugin’s source and commit in `.claude-plugin/marketplace.json`. Enable its `plugin@marketplace` ID in the host settings for that repository (`.codex/config.toml` or `.claude/settings.json`). To add another standards publisher, add its entry and enable its ID. The publisher chooses which instructions its native `SessionStart` hook delivers. TJ checks the selection for the checkout where the session opens, including linked worktrees.

A project can also use a publisher’s marketplace directly. It declares that source under `marketplaces` in Codex or `extraKnownMarketplaces` in Claude, then enables the corresponding plugin ID. This works in a repository with only the provider settings. For example, a writing project can select its own style plugin while a software project selects a different publisher in the same host profile. Run `tj plugins update --dry-run --host codex` in either checkout to see its selection. A pinned source gives the check an exact required revision. A floating Git source follows the publisher when updated. If the host profile already provides a Git-backed marketplace, the project can simply enable its plugin ID. TJ uses the marketplace revision available to that profile.

### Personal standards

A repository can adopt a personal standards plugin by selecting it the same way. Its rules then apply in that project. An individual can also enable a plugin in their own native profile. That is an account-level choice and may affect other projects using the same profile. TJ’s project check covers project-selected plugins. The migration plan tracks explicit private, user- or task-scoped policy separately. [Personal standards plugin](https://github.com/tjentic/personal-standards)

Projects using the older Markdown updater should remove its delimited section from their instruction files when adopting these plugins. Keep handwritten instructions around that section. The old `.tjentic/standards.toml` selected individual documents. The selected plugin delivers the instructions maintained by its publisher.

[Migration plan](plans/migration/PLAN.md) · [Contributor instructions](AGENTS.md)
