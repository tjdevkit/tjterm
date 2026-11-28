# TJentic

TJentic is a terminal tool in development for working with Claude Code and Codex in your own projects. The first milestone is finding an unfinished conversation and resuming it in the Git worktree where you left it.

The executable is `tj`. This preview can synchronize shared project standards.

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

Codex loads the section according to its usual scope and gives `AGENTS.override.md` precedence. Claude Code can use the same file through native discovery, an existing `CLAUDE.md` → `AGENTS.md` symlink, or a local `@AGENTS.md` import in a separately authored `CLAUDE.md`. Where Claude already uses `CLAUDE.md`, link or import the corresponding `AGENTS.md` at each scope Claude should read. Start in the relevant subtree when you need its rules on the first turn.

[Migration plan](plans/migration/PLAN.md) · [Contributor instructions](AGENTS.md)
