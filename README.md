# TJentic

TJentic is a terminal tool in development for working with Claude Code and Codex in your own projects. The first milestone is finding an unfinished conversation and resuming it in the Git worktree where you left it.

The executable is `tj`. Each Git checkout selects its own contributor plugins for Claude Code and Codex.

## Set up contributor plugins

From the checkout you plan to work in, install its selected plugins for your host.

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
