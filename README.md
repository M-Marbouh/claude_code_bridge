# Claude Code Bridge Fork

Lightweight multi-agent terminal coordination for a single-machine workflow.

This fork is based on the v5 line of `SeemSeam/claude_code_bridge` (formerly `bfly123/claude_code_bridge`, now redirected). It keeps the original split-pane model and focuses on a practical Linux setup: Claude, Codex, Gemini, OpenCode, Droid, Qwen, and related CLI agents running visibly in WezTerm or tmux panes.

## What It Is

CCB starts and manages AI provider panes, then lets one provider send work to another through simple CLI commands such as `ask`, `pend`, and provider-specific ping tools.

This fork is intentionally smaller in scope than a distributed agent platform:

- Single machine
- Linux-first
- WezTerm or tmux terminal panes
- Visible, controllable provider sessions
- Local runtime state under the CCB run directory
- v5-compatible architecture with fork-specific stability fixes

## Who It Is For

This fork is for solo developers who use Claude as the primary planning and review agent, with Codex or other terminal agents handling implementation, investigation, or second opinions.

It is a good fit when you want:

- Claude + Codex collaboration in separate panes
- Async delegation without API keys or hidden remote workers
- Cross-project handoffs between active local CCB sessions
- A lightweight workflow that can be inspected from the terminal

It is not trying to be a team orchestration server, cloud queue, or replacement for each provider's native CLI.

## Install

Install from the `M-Marbouh/claude_code_bridge` fork:

```bash
curl -fsSL https://raw.githubusercontent.com/M-Marbouh/claude_code_bridge/main/install.sh | bash -s -- install
```

After install, start CCB from a project directory:

```bash
ccb
```

The live install is copied to `~/.local/share/codex-dual/`. Make source changes in the development repo, then deploy through `install.sh`; do not edit the live install directly.

## What's Different

This fork diverges from upstream v5 in a few practical areas:

- **Runtime mount-awareness (new in `0.11.0`)**: `ccb-list` and `ccb-mounted` report each qualified provider's real state (capable / configured / mounted). `ask codex:worker` routes to that instance if mounted and fails loudly with `CCB_ROUTE_ERROR` if not — no silent base fallback.
- **Multi-instance providers (new in `0.10.0`)**: run a second pane of the same provider (`codex:worker`, `claude:worker`) with its own session, resume, and pane. Configure in `.claude/ccb.config`; no built-in model defaults — supply `instances` overrides in `ccb.config` if needed. `CCB_CODEX_SHOW_TIER=1` prints the live model/effort.
- Gemini `CCB_DONE` handling is hardened for replies that omit or misplace completion markers.
- Self-update URLs point at the `M-Marbouh/claude_code_bridge` fork.
- `ccb-list` lists active local CCB projects and provider pane liveness.
- `ccb-bridge-ask` sends a message to Claude in another active CCB project.
- `ask --peer <target>` delegates cross-project messages through the bridge.

The fork is not tracking upstream v6 behavior. It keeps the v5-style local terminal workflow and adds features around reliability and cross-project coordination.

## Usage

Start a CCB session in a project:

```bash
cd ~/dev/my-project
ccb codex claude
```

Ask another provider from the current CCB session:

```bash
ask codex "Investigate the failing test and report the likely cause"
ask gemini "Review this implementation plan for edge cases"
pend codex
```

### Model-tiered workers

Declare extra instances of a provider in `ccb.config` (or on the command line). A `:worker` token spawns a second, cheaper pane:

```
codex, codex:worker, claude, claude:worker
```

`:worker` panes auto-resolve to cheap defaults (`codex` → gpt-5.4-mini, `claude` → Haiku) with no model strings required; the architect/orchestrator panes (`codex`, `claude`) stay on the strong model. Send work to a worker the same way you reach any provider:

```bash
ask codex:worker "Apply this patch and run the test suite"
ask claude:worker "Update the changelog and project memory"
```

Confirm the live model/effort of a Codex pane (off by default, so normal output is unchanged):

```bash
CCB_CODEX_SHOW_TIER=1 ask codex:worker "noop"
# reply ends with: [codex:worker model=gpt-5.4-mini effort=medium sandbox=workspace-write]
```

Only `codex` and `claude` support instances today. The feature is inert unless you declare a `:worker`, so plain `ccb codex` is unchanged. Override a worker's model per project with an `instances` map in `ccb.config` (JSON form).

List active CCB projects:

```bash
ccb-list
ccb-list --json
```

Bridge to Claude in another active project:

```bash
ask --peer ~/dev/content-automation "What are you currently working on?"
ask --peer b0e3 "Review this handoff"
ask claude --peer 1 "Summarize the current state"
```

Targets for `--peer` can be:

- Exact project path
- `ccb-list` index
- CCB project hash prefix with at least 4 hex characters

## Roadmap

The current milestone is `v1.0.0`.

Recently shipped:

- `0.11.8` — finished the Mutual Ratification rollout: retired the orphaned scored-Rubric templates in `config/agents-md-ccb.md`'s AGENTS.md block, and migrated the `all-plan` skill (all three provider copies) from dimension-scored review to ratification verdicts (concur/concur-with-amendment/contest/insufficient-evidence).
- `0.11.7` — retired the scored "Peer Review Framework" for **Mutual Ratification**: Claude's inspection/review conclusions are proposals (claim + evidence + intended action), not directives, and Codex ratifies (concur/amend/contest) before acting and before fixes land. Wired into the `delegate` skill's briefing standard.
- `0.11.6` — removed built-in `codex:worker` and `claude:worker` default policies; worker pane model/effort now opt-in via project `ccb.config` instance overrides.
- `0.11.5` — removed dead `bin/laskd` + `lib/laskd_daemon.py` (superseded by the unified `askd`; nothing imported or spawned them). The installer already lists them as legacy, so this makes that cleanup effective instead of a no-op.
- `0.11.4` — installer no longer auto-allows `Bash(ask *)` (sending an ask is an outward action worth confirming) and actively removes a prior injection on re-install, so the confirmation prompt survives deploys.
- `0.11.3` — default `codex:worker` reasoning effort raised `medium → xhigh` (more WHAT→HOW bridging IQ for bounded implementation; preserves the mini cost tier). Override per project via `ccb.config` `instances`.
- `0.11.2` — multi-instance session isolation: `ccb kill` now enumerates every project session file (worker instances included, qualified args like `codex:worker` accepted); `ccb -r` resumes each pane from its validated bound session id (launch-time binding + anchor-confirmed repair) instead of latest-by-cwd/`--continue`, so architect/orchestrator no longer inherit a worker's conversation; Codex reader hardened against cross-instance log pickup.
- `0.11.1` — `ccb clean` plus conservative auto-prune of stale `ccb-session-ai-*.json` records (keep newest N per project, TTL, and a liveness gate that never deletes a live/running session). Auto-prunes the launching project on startup unless `CCB_NO_AUTO_PRUNE=1`.
- `0.11.0` — runtime-status primitive behind `ccb-list`/`ccb-mounted` (per-qualified-key capable/configured/mounted, robust to stale session files) and honest `[WORKER]`/`[ARCHITECT]` tag routing with structured `CCB_ROUTE_ERROR` / `CCB_ROUTE_FALLBACK` (no silent base fallback).
- `0.10.0` — multi-instance, model-tiered providers (`codex:worker`, `claude:worker`) with per-instance session, pane, and resume isolation, and a `CCB_CODEX_SHOW_TIER` verification footer.

Planned before `v1.0.0`:

- Harden `ccb-bridge-ask` target resolution and stale pane diagnostics.
- Per-instance resume polish and per-project model/effort overrides.
- Document common Claude + Codex workflows and single-machine Linux examples.
- Keep the fork's README, changelog, and versioning independent from upstream.

Current pre-release version: `0.11.8`.
