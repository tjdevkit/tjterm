# TJ Term migration plan

## Current checkpoint

**26 November 2026. The setup change is [PR #1](https://github.com/tjdevkit/tjterm/pull/1).** TJ Term is the product and `tj` is the executable. The existing installation supplies the daily command during development.

| Change | Delivered scope | Next fresh context |
| --- | --- | --- |
| Setup PR #1 | Nine files covering shared contributor instructions, a pinned Python development environment, this plan and scaffold CI | Verify the merged PR and its final checks. Reproduce one selected plugin update from revision A to B with an unchanged control. Record the first difference between selection, installed files and behaviour in the running host. |

On Arch Linux, a fresh clone passed the locked environment sync with Python 3.12.16 and uv 0.13.7, the Ruff configuration check, the contributor symlink and plan checks, and workflow linting. GitHub's Ubuntu and Windows jobs both passed on scaffold revision `4b852c6`. The PR records the merge commit and the checks on the final revision. [Hosted scaffold checks](https://github.com/tjdevkit/tjterm/actions/runs/45877677855) · [Setup PR](https://github.com/tjdevkit/tjterm/pull/1)

This file is the project handoff. Each new context reads the root `AGENTS.md`, checks this section against Git and CI, and resumes the single agreed stage. At a major merge, record the PR or commit, the tested build, the platform and provider versions, what worked, what was discarded and the next action. Keep runtime imports for a separate review after the investigation.

## First merge

Start with clean Git history. A short README explains the intended terminal workflow. Both providers read one contributor policy via `AGENTS.md` and the relative `CLAUDE.md` symlink. Adopt Python 3.12+, uv, strict typing, Ruff and isolated pytest settings for development from the beginning. Validate the lock and configuration on Ubuntu and Windows. At the first actual Python change, enable formatting, lint, static types, real tests and the house source checker. Demonstrate that a failing example is caught. The root instructions give the exact commands and pinned source. [Contributor instructions](../../AGENTS.md)

The first merge ends at project setup. Runtime code, installers, host configuration, plugin publication, personal machine changes, standards repository creation and archival each have a later decision point.

## The developer's workflow

| Tomorrow morning | Where the developer goes | What TJ Term must know |
| --- | --- | --- |
| Open the project | **Workspaces** | Resolve the Git repository and its linked worktrees. |
| Find an unfinished change | **Work items**, filtered to that workspace | Show open items, their state and linked conversations. A board presents these same records and transitions. |
| Pick up the conversation | Open the item's **Sessions**, or search Sessions directly when there is no tracker item | Show the provider, native session ID and recorded worktree. Report a missing directory before offering recovery choices. |
| Finish the work | Resume in the native terminal session, then inspect the completion receipt and item | Record the actual outcome. Preserve a worktree that still has authored changes or an explicit hold. |

Keep these three views in one product repo. Add a board or TUI when the underlying commands can complete the workflow. Inspect the existing Workboard picker before designing a new one. [Workboard picker](https://github.com/tomjseery/agent-workboard/blob/main/crates/cli/src/board.rs)

## Existing code and where it belongs

The existing implementations below are candidates. Port a component when a user action needs it and an installed build demonstrates the result.

| Existing source | What it does today | Destination and reason |
| --- | --- | --- |
| CLI | Rust executable and launcher, transcript readers, directory search, process supervision, messages and completion receipts. Its Python package already exposes process and path operations to Agent. | Bring the session reader and exact directory evidence into Sessions first. Preserve the command contracts as each capability arrives. Assess folder jumps, shell shortcuts and other utilities individually. |
| Agent | Managed Claude/Codex launch, session hooks, handoff, recovery, plugin selection, update and machine provisioning share an installer. | Put the provider-specific lifecycle operations behind the product commands. Package the minimum required bridge with TJ Term. Split selected plugin maintenance from machine provisioning. |
| Agent Store | SQLite records work items, launches, native session bindings and worktree claims. Host state supplies liveness. | Reuse the proven data model and transactional claims for Sessions and Work items. Retain compatibility with existing local records at cutover. A separate service adds no current requirement. |
| Work Items | Shared tracker references, provider adapters and project-specific tracker profiles. | Put neutral operations and reference parsing under Work items. Exercise GitHub first. Bring across Azure DevOps when its workflow can be tested. An organisation's tracker policy belongs in its own profile. |
| Core | Mixed development instructions, workflow rules and executable operations. The legacy item-start path calls the worktree creator through the enabled Base plugin. | Move the necessary Git operation to Workspaces. Keep planning, review and delegation preferences with their owner. |
| Personal and individual stack producers | Explicit project adoption, language rules, personal tracker choices and optional machine identity setup. | Publish separate `python-standards`, `rust-standards`, `cpp-standards` and `personal-standards` as they are adopted. Separate release histories let one project select only the stacks it uses and update just the package that changed. |
| Marketplace and Template | One catalogue records sources, dependencies and pins. Generation produces Claude and Codex manifests from authored content. | Keep one source for each reusable standards package and generate host packaging when needed. Reuse the existing catalogue if discovery needs it. Defer a new framework, universal plugin format and standalone updater until another real caller needs them. |
| Docs | Product vision, project selection model and operational decisions. | Keep relevant product behaviour and migration evidence in this repo. Put a personal or work policy beside the package that owns it. |

## Native hosts, standards and refresh

**TJ Term should work in a plain repository with the user's own Claude or Codex account and zero personal plugins.** In the old code, a project without a plugin selection can skip standards checks and the CLI can search existing transcripts. Reliable managed binding and completion use Agent's lifecycle hooks, recovery uses its recorded bindings and working directory, and item-start needs the worktree creator from Base. The old full installer also pulls a private Work Items source. Treat a clean public install, independent launch and exact resume on each provider as acceptance tests for the replacement.

| Operation | Existing coupling | Direction |
| --- | --- | --- |
| Launch or recover a session | The supervisor starts the native host in the intended directory. Registration binds the launch to the provider ID. Recovery needs that binding, a transcript, an existing worktree and host liveness. A fresh project need not adopt the personal standards profile. | Ship essential provider bridges with the product. The user's native account, project instructions and permission choices continue to apply. Test normal use without personal plugins or changes to the user's shell startup. |
| Deliver a message | The current `tj message` routes Claude-to-Claude through the sending Claude session's native adapter. Codex and cross-host cases use a per-session feed. A stored feed entry and a native submission provide different evidence. | Keep one executable command. If Codex gains equivalent native delivery, change the Codex adapter and its capability tests. Preserve request identity, explicit outcomes and protection against double delivery. |
| Refresh a selected plugin | The existing updater checks source revisions and installed content, verifies host admission and reloads supported runtimes. It also invokes Agent installation to converge the CLI, Store, bindings and profiles. An embedded Codex process can require a restart. | Resolve selected packages and their dependency closure. Advance only changed pins. Report installation and live activation individually, preserve unsent drafts and expose the real host reload scope. Reuse one implementation if a standalone command and `tj plugins update` both need it. |
| Sync Tommy's machine or project policy | The installer also provisions shell profiles, Codex lanes, other tools and command bindings. Personal adoption can generate preferred instructions and configure identity. | Keep machine sync opt-in under Tommy's control. Let each repo select work or personal rules. Use checked-in Markdown for ordinary guidance. A plugin earns its place for reusable packaging, native discovery or executable integration. |

An account or checkout path cannot determine the user's preferred standard or tracker. Keep the same authored rules across hosts wherever possible. A runtime operation needed for correctness must work when no agent reads a skill.

Project setup should remember a developer's explicit choices across worktrees. The first push here exposed two independent prerequisites for the optional publishing profile. GitHub had to grant the selected App access to this repository, and the local Git helper had to recognise its owner. Check both when configuring a publishing destination, show which one needs attention and reuse the verified setup on subsequent pushes. Opening or resuming a local session requires neither publishing permission.

## First investigation after setup

Tommy reports longstanding bugs in terminal behaviour and plugin refresh. Duplicate instructions, stale executables and outdated installed plugin content are hypotheses. In a disposable checkout with isolated provider state, record the first discrepant observation before deciding what implementation to port.

| Question | Evidence |
| --- | --- |
| Which executable and adapter ran? | Record the path and version from a fresh terminal, the resolved shim, and the build the host loaded. Repeat with the old checkout and caches unavailable. |
| Where did each instruction come from? | Put distinct harmless markers in a project instruction and an optional plugin. Check each host with that plugin selected and unselected. Identify the source of any duplicate or stale marker. |
| Where did refresh stop? | Stage synthetic versions A and B of one selected plugin alongside an unchanged control. Record selected pin, installed bytes, running activation, next-turn behaviour and preservation of an unsent draft. Repeat with no changes and with a failed reload. |
| Which recovery failures reproduce? | Check preview mutating state, a manually closed Linux tab reopening, a child taking its parent's binding and a vanished worktree falling back to the caller's directory. Report the tested version even when the case passes. |

## Candidate feature merges

The first useful milestone is finding yesterday's conversation and resuming it in the exact worktree on Arch. Aim to demonstrate that slice within the first few focused days after setup. Record real Claude, Codex and Windows runs before claiming support on each.

| Increment | Installed acceptance |
| --- | --- |
| Session search | Find a conversation by something said in it. Distinguish similarly named conversations in two worktrees by provider, ID and directory. Read a transcript with a torn final line. |
| Managed launch and exact resume | Launch in a linked worktree, make a turn, stop, then resume from another directory in a fresh terminal. Refuse a conflicting writer. Report a vanished worktree without launching in the wrong place. |
| Manual plugin update | Use Rust-only, Python-only and mixed projects. A Rust update changes Rust alone. Show installed and active versions. Preserve the last successful state on failure. Decide on background updates after the manual path works. |
| Messaging and handoff | Exercise all four host pairs. Distinguish stored, submitted, refused and unknown. Retrying a request or completing a parented handoff delivers the result once. |
| Work items and completion | Open an item, launch or resume its session in the linked worktree, finish from the terminal and inspect the receipt and tracker transition. A hold, missing final result or authored changes preserves the worktree. A merged PR alone cannot finish a session. |
| Cutover and archival | Test a clean Windows and Linux install with no private or legacy source access. Check repo URLs, release downloads and asset names, installer and CI pins, plugin identities, shims and checkout paths. The legacy Work Items installer pin `39c7c87` did not resolve during the audit. Test an upgrade against a backed-up database and return to the prior working build. Review old public visibility with Tommy before archiving. |

## Keeping each merge reviewable

1. Choose one user action. Compare the current provider capability and existing implementation. Record the reason for each component reused, replaced or discarded. Preserve the relevant attribution.
2. Exercise the installed candidate beside the working release using isolated state. Give Tommy one repeatable demonstration and the observed result. Record automated checks and live host runs individually.
3. Remove superseded paths, hooks, obsolete configuration and unused dependencies from the candidate. If it breaks a proven case, repair or revert before moving on.
4. Update the checkpoint with the evidence and next action. Start a fresh context.

T3 Code co-locates provider adapters and their tests and supplies a fake CLI for repeatable process tests. Adopt that boundary for changes such as a native Codex messaging transport, then exercise the real terminal. [T3 Code adapters](https://github.com/pingdotgg/t3code/tree/f28ced81b36027597c2fe45a1cc271e880975451/apps/server/src/orchestration-v2/Adapters) · [Fake CLI](https://github.com/pingdotgg/t3code/blob/f28ced81b36027597c2fe45a1cc271e880975451/packages/provider-testing/src/fakeCli.ts)

Vibetonomy's published launcher chooses a platform binary, supplies its bundled Claude bridge and inherits terminal input/output. Its explicit binary selection and missing-component error are worth comparing when investigating stale installations. [Published launcher](https://cdn.jsdelivr.net/npm/@vibetonomy/agent@1.7.8/bin/launch.js) · [Package manifest](https://registry.npmjs.org/@vibetonomy%2fagent/1.7.8)
