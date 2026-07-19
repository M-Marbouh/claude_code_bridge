# Changelog

## Unreleased

### Changed

- Made implicit `pend` lookup strict to the current CCB tab, allowed paired panes to retrieve the same tab's tasks, and normalized `peer-<provider>` receipts by their responding provider.
- Added context-first exact task guidance plus `pend peer`, `pend local`, and newest-first task history with task-ID prefixes. `pend <provider> N` now reads task receipts; legacy provider conversation history requires explicit `--legacy`.
- Made pre-restart and otherwise unbound receipts exact-task-ID-only, with clear implicit-lookup guidance, while preserving the same-turn Async Guardrail.
- Added provider-aware cross-project peer routing for mounted Codex panes using the same explicit reverse-reply protocol as Claude.
- Made all peer delivery one-shot so later Codex pane responses cannot be captured and forwarded automatically.
- Added `peer_providers` discovery, provider-scoped bridge locks, and sender-project completion fallback routing.
- Added explicit `--wait`, `--background`, and `--notify` intent for cross-project Claude messages.
- Made background peer consultations non-blocking and reverse peer results one-way, preventing unrelated plans from being abandoned while waiting.
- Treat successful pane injection as peer-delivery success even when Claude's session-log anchor is delayed or unavailable.
- Added `--reply-to` task correlation and rejected notifications that end with reply-requiring questions.
- Made explicit peer replies retain a validated caller-pane return route when the sender drops out of live project discovery.
- Preserved correlated peer results for `pend <task-id>` recovery when the original pane is no longer deliverable.
- Added an authenticated filesystem-mailbox transport for managed Codex, allowing sandboxed `ask`, `ccb-list`, and peer messaging to reach the host `askd` without terminal-socket or loopback-network access.
- Made sandboxed discovery fail explicitly when the running daemon predates mailbox support instead of returning a misleading empty project list.
- Delegated provider runtime checks for `ask`, `ccb-ping`, and `ccb-mounted` to the host daemon so managed Codex does not misclassify live WezTerm or tmux panes as dead.
- Routed one-way Claude and Codex `ask --notify` requests through unified `askd` instead of the sandbox-incompatible legacy terminal path.
- Scoped implicit `pend <provider>` selection by the concrete CCB session instead of persisted caller identity, allowing paired Claude and Codex panes to share tab tasks without crossing into another tab.
- Isolated test subprocess temporary directories so peer-task tests no longer create receipts in the user's live `/tmp/ccb-tasks` directory.
- Made `ask`, `ccb-list`, runtime preflight, and peer delivery discover project-scoped `askd` state from the current working directory when `CCB_RUN_DIR` is not inherited.
- Added automatic TCP-to-mailbox fallback for daemon RPC clients, covering restricted Claude and Codex command subprocesses without provider-specific environment assumptions.
- Extended `askd` startup readiness to a configurable 10-second window, added a deadline-edge health check, and reported early daemon process exits distinctly.
- Made the launcher reuse its own live `askd` child across provider startup hooks and stop reporting lock-contention duplicates as daemon failures, while requiring a successful RPC before declaring readiness.
- Retried valid per-project daemon probes before reporting `daemon_offline`, persisted launcher PIDs in registry records, and excluded orphaned provider panes from active peer discovery.
- Restored TCP daemon response accumulation and added deterministic TCP and mailbox round-trip coverage so runner environment variables cannot silently select the wrong transport.
- Made implicit managed `ask`, `ccb-ping`, and `ccb-mounted` status checks use the reachable daemon's validated project root, preventing subdirectory cwd hashes from producing false `not_mounted` or `daemon_offline` results.

## v0.12.0 (2026-07-11)

### Changed

- Retired provider instances, `:worker` targets, routing tags, abstract roles, and sub-agent skills.
- Reduced the supported provider set to Claude, Codex, Gemini, and OpenCode.
- Added structured async task receipts and deterministic `pend <task-id>` retrieval.
- Scoped `pend <provider>` to the current caller session and fail on ambiguous same-project callers.
- Made `ask` preflight the provider session, pane, binding, and daemon before reporting async submission.
- Required a matching `CCB_REQ_ID` anchor before Codex may switch or persist a session-log binding.
- Preserved Linux, Windows/PowerShell, WezTerm, and tmux support.

## v5.2.8 (2026-03-07)

### 📝 Documentation

- **tmux Layout Tip**: Added English and Chinese usage notes explaining that `Ctrl+b` then `Space` cycles tmux layouts and can be pressed repeatedly

## v5.2.7 (2026-03-07)

### 🔧 Stability Fixes

- **Completion Status**: Completion hook now distinguishes `completed`, `cancelled`, `failed`, and `incomplete` instead of reporting every terminal state as completed
- **Cancellation Handling**: Gemini and Claude adapters now consistently honor cancellation and emit a terminal status instead of leaving requests stuck in processing
- **Routing Safety**: Completion routing now keeps parent-project to subdirectory compatibility while preventing nested child sessions from hijacking parent notifications
- **Codex Session Binding**: Bound Codex requests no longer drift to a newer session log in the same worktree
- **askd Startup Guardrails**: `bin/ask` now respects `CCB_ASKD_AUTOSTART=0` and scrubs inherited daemon lifecycle env before spawning askd
- **Claude Session Backfill**: `ccb` startup again backfills `work_dir` and `work_dir_norm` into existing `.claude-session` files
- **Regression Tests**: Added focused tests for completion status handling, caller routing, autostart behavior, cancellation paths, and Codex session binding

## v5.2.5 (2026-02-15)

### 🔧 Bug Fixes

- **Async Guardrail**: Added global mandatory turn-stop rule to `claude-md-ccb.md` to prevent Claude from polling after async `ask` submission
- **Marker Consistency**: `bin/ask` now emits `[CCB_ASYNC_SUBMITTED provider=xxx]` matching all other provider scripts
- **SKILL.md DRY**: Ask skill rules reference global guardrail with local fallback, eliminating duplicate maintenance
- **Command References**: Fixed `/ping` → `/cping` and `ping` → `ccb-ping` in docs

## v5.2.4 (2026-02-11)

### 🔧 Bug Fixes

- **Explicit CCB_CALLER**: `bin/ask` no longer defaults to `"claude"` when `CCB_CALLER` is unset; exits with an error instead
- **SKILL.md template**: Ask skill execution template now explicitly passes `CCB_CALLER=claude`

## v5.2.3 (2026-02-09)

### 🚀 Project-Local History + Legacy Compatibility

- **Local History**: Context exports now save to `./.ccb/history/` per project
- **CWD Scope**: Auto transfer runs only for the current working directory
- **Legacy Migration**: Auto-detect `.ccb_config` and upgrade to `.ccb` when possible
- **Claude /continue**: Attach the latest history file with a single skill

## v5.2.2 (2026-02-04)

### 🚀 Session Switch Capture

- **Old Session Fields**: `.claude-session` now records `old_claude_session_id` / `old_claude_session_path` with `old_updated_at`
- **Auto Context Export**: Previous Claude session is extracted to `./.ccb/history/claude-<timestamp>-<old_id>.md`
- **Transfer Cleanup**: Improved noise filtering while preserving tool-only actions

## v5.1.2 (2026-01-29)

### 🔧 Bug Fixes & Improvements

- **Claude Completion Hook**: Unified askd now triggers completion hook for Claude
- **askd Lifecycle**: askd is bound to CCB lifecycle to avoid stale daemons
- **Mounted Detection**: `ccb-mounted` now uses ping-based detection across all platforms
- **State File Lookup**: `askd_client` falls back to `CCB_RUN_DIR` for daemon state files

## v5.1.1 (2025-01-28)

### 🔧 Bug Fixes & Improvements

- **Unified Daemon**: All providers now use unified askd daemon architecture
- **Install/Uninstall**: Fixed installation and uninstallation bugs
- **Process Management**: Fixed kill/termination issues

### 🔧 ask Foreground Defaults

- `bin/ask`: Foreground mode available via `--foreground`; `--background` forces legacy async
- Managed Codex sessions default to foreground to avoid background cleanup
- Environment overrides: `CCB_ASK_FOREGROUND=1` / `CCB_ASK_BACKGROUND=1`
- Foreground runs sync and suppresses completion hook unless `CCB_COMPLETION_HOOK_ENABLED` is set
- `CCB_CALLER` now defaults to `codex` in Codex sessions when unset

## v5.1.0 (2025-01-26)

### 🚀 Major Changes: Unified Command System

**New unified commands replace provider-specific commands:**

| Old Commands | New Unified Command |
|--------------|---------------------|
| `cask`, `gask`, `oask`, `dask`, `lask` | `ask <provider> <message>` |
| `cping`, `gping`, `oping`, `dping`, `lping` | `ccb-ping <provider>` (skill: `/cping`) |
| `cpend`, `gpend`, `opend`, `dpend`, `lpend` | `pend <provider> [N]` |

**Supported providers:** `gemini`, `codex`, `opencode`, `droid`, `claude`

### 🪟 Windows WezTerm + PowerShell Support

- Full support for Windows native environment with WezTerm terminal
- `install.ps1` now generates wrappers for `ask`, `ccb-ping`, `pend`, `ccb-completion-hook`
- Background execution uses PowerShell scripts with `DETACHED_PROCESS` flag
- WezTerm CLI integration with stdin for large payloads (avoids command line length limits)
- UTF-8 BOM handling for PowerShell-generated session files

### 🔧 Technical Improvements

- `completion_hook.py`: Uses `sys.executable` for cross-platform script execution
- `ccb-completion-hook`:
  - Added `find_wezterm_cli()` with PATH lookup and common install locations
  - Support `CCB_WEZTERM_BIN` environment variable
  - Uses stdin for WezTerm send-text to handle large payloads
- `bin/ask`:
  - Unix: Uses `nohup` for true background execution
  - Windows: Uses PowerShell script + message file to avoid escaping issues
- Added `SKILL.md.powershell` for `cping` and `pend` skills

### 📦 Skills System

New unified skills:
- `/ask <provider> <message>` - Async request to AI provider
- `/cping <provider>` - Test provider connectivity
- `/pend <provider> [N]` - View latest provider reply

### ⚠️ Breaking Changes

- Old provider-specific commands (`cask`, `gask`, etc.) are deprecated
- Old skills (`/cask`, `/gask`, etc.) are removed
- Use new unified commands instead

### 🔄 Migration Guide

```bash
# Old way
cask "What is 1+1?"
gping
cpend

# New way
ask codex "What is 1+1?"
ccb-ping gemini
pend codex
```

---

For older versions, see [CHANGELOG_4.0.md](CHANGELOG_4.0.md)
