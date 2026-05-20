# Claude Code Bridge Fork

Lightweight multi-agent terminal coordination for a single-machine workflow.

This fork is based on the v5 line of `bfly123/claude_code_bridge`. It keeps the original split-pane model and focuses on a practical Linux setup: Claude, Codex, Gemini, OpenCode, Droid, Qwen, and related CLI agents running visibly in WezTerm or tmux panes.

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

Planned before `v1.0.0`:

- Stabilize the `ccb-list` output contract.
- Harden `ccb-bridge-ask` target resolution and stale pane diagnostics.
- Document common Claude + Codex workflows.
- Add public-facing examples for single-machine Linux setups.
- Keep the fork's README, changelog, and versioning independent from upstream v5/v6.

Current pre-release version: `0.9.0`.
