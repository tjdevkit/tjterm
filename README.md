# TJentic

TJentic is a terminal tool in development for working with Claude Code and Codex in your own projects. The first milestone is finding an unfinished conversation and resuming it in the Git worktree where you left it.

The executable is `tj`. This project selects Python and Rust contributor plugins for Claude Code and Codex.

## Set up contributor plugins

From the checkout you plan to work in, install its selected plugins for your host.

```sh
tj plugins update --host codex
tj plugins check --host codex
```

For Claude Code, use `--host claude`. Use `--dry-run` with `update` to see the selected commits. In Codex, review the plugin commands through the native hook trust prompt or `/hooks`. Start a new session or resume the intended one after installation. Each plugin supplies its own conventions through its native `SessionStart` hooks.

Both hosts use the project marketplace in `.claude-plugin/marketplace.json`. It records each upstream source and commit once. The host settings in `.codex/config.toml` and `.claude/settings.json` enable the selected IDs in this checkout. The check inspects the installed revisions and native startup hook configuration.

Projects using the older Markdown updater should remove its delimited section from their instruction files when adopting these plugins. Keep handwritten instructions around that section. The old `.tjentic/standards.toml` selected individual documents. A language plugin delivers the convention set maintained by its publisher.

[Migration plan](plans/migration/PLAN.md) · [Contributor instructions](AGENTS.md)
