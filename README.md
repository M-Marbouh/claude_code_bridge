# Claude Code Bridge Fork

Lightweight, single-machine coordination for Claude, Codex, Gemini, and OpenCode.

This fork follows the upstream v5 terminal-pane architecture while keeping a deliberately small operating model:

- One instance of each provider per project
- No workers, provider suffixes, abstract roles, or sub-agents
- Visible sessions in WezTerm or tmux
- Linux shell and Windows/PowerShell support
- Request-scoped async results

## Install

Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/M-Marbouh/claude_code_bridge/main/install.sh | bash -s -- install
```

Windows PowerShell:

```powershell
git clone https://github.com/M-Marbouh/claude_code_bridge.git
cd claude_code_bridge
.\install.ps1 install
```

The live install is stored under `~/.local/share/codex-dual/` on Linux. Develop in the repository and deploy through the installer; do not edit the live copy directly.

## Start a project

```bash
cd ~/dev/my-project
ccb codex claude
```

Available providers are `claude`, `codex`, `gemini`, and `opencode`. A project may run any subset:

```bash
ccb codex claude gemini opencode
```

Starting another CCB session for the same directory reuses the existing provider pane when possible and otherwise fails clearly. Qualified names such as `codex:worker` are rejected.

## Ask and retrieve results

```bash
ask codex "Investigate the failing test"
ask gemini "Check this explanation for missing cases"
ask opencode "Review this patch"
```

Async submission prints a task ID and stores a structured receipt. Retrieve the exact result with:

```bash
pend 20260711-120000-001-99
```

For convenience, this resolves the latest task submitted by the current CCB caller session:

```bash
pend codex
```

If multiple same-project caller sessions are possible and caller identity is unavailable, `pend` reports ambiguity and lists task IDs instead of guessing.

Legacy conversation readers remain available temporarily:

```bash
pend codex --legacy
```

## Runtime diagnostics

```bash
ccb-list
ccb-list --json
ccb-mounted
ccb-ping codex
```

`ccb-mounted` is a human diagnostics command. Delegation does not require a separate mounted skill: `ask` validates the provider session, pane, binding, and daemon before reporting successful async submission.

## Cross-project Claude messaging

```bash
ask --peer ~/dev/another-project "Summarize the current state"
ask --peer b0e3 "Review this handoff"
```

Targets may be an exact path, a `ccb-list` index, or a project-hash prefix of at least four characters.

## Session safety

CCB groups runtime records by project path but routes requests using the concrete CCB session and caller pane whenever available. Codex log binding is updated only after the target log contains the exact `CCB_REQ_ID` request anchor; a newer standalone Codex conversation in the same folder cannot win merely because it has a later timestamp.

## Configuration

The project configuration lives at `.ccb/ccb.config`:

```text
codex,claude
```

Provider instances and `instances` overrides are no longer supported. Existing `provider_instances` and `instances` keys are ignored during configuration normalization, and stale worker session files are not loaded.

## Maintenance

```bash
ccb clean --dry-run
ccb clean
ccb kill
ccb version
```

## Fork changes

- `0.12.0` — returned to a single-instance architecture; retained Claude, Codex, Gemini, and OpenCode; added request-scoped task receipts and deterministic `pend`; removed worker/tag/role/sub-agent behavior; hardened same-folder Codex isolation.
- `0.11.x` — introduced runtime status, cleanup, and experimental multi-instance work. Multi-instance behavior was retired in `0.12.0`.
- `0.9.0` — added local project listing and cross-project Claude messaging.

This fork intentionally remains a local terminal tool, not a distributed orchestration platform.
