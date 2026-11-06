# Changelog

## Unreleased

### Added

- Added an opt-in role delivery gate: `ccb --role-policy enforce` (or `role_policy = "enforce"` in `ccb.config`) refuses asks whose sender role may not address the destination role, using a fixed matrix (`lead` may address `implementer` and `ratifier`; both may address `lead`; the two may not address each other; a session with no role neither sends nor receives; external senders may only reach `lead`). The gate applies at the daemon accept step, again immediately before pane injection, and on the bridge direct-reply path; email ingress is always treated as external. Verified correlated peer replies still reach their original requester whatever role it now holds. Launches without the setting behave as before.
- Added `ask --to-role <role>`, which asserts the destination session's role and is always verified, with or without enforcement.
- Added the read-only `check_delivery` daemon operation so an async submission reports a policy refusal instead of announcing a submission that will not be delivered.
- Added launch-time role assignment from `ccb.config`: a JSON `"roles": {"claude": "lead", "codex": "ratifier"}` object (a list in launch order for the Codex pair) or `provider=role` tokens in a plain-text config. The launcher claims each role for its session through the normal claim checks once the pane is ready; invalid role settings stop the launch before any pane starts, and a claim that fails is reported and leaves the launch running as `DEGRADED`. Configs without roles behave as before. Known limitation: on WezTerm, roles stop counting once Claude Code or Pi retitle their pane; use tmux for role-enforced launches until this is fixed.

### Fixed

- In a Codex-pair launch whose last provider is not Codex or Claude (for example `codex, codex, pi`), that session could never record a role, even with `ccb-role set`, because its launch entry was not refreshed after its pane started. The launcher now refreshes it before the session starts.
- A Pi session started in the launcher's own WezTerm pane now sets its pane title marker, as the other providers already did.
- A background (`async_submit`) ask no longer reports `accepted: true` when the request had already been refused before queueing; it now returns the refusal and a nonzero exit code.

- Any CCB launch can now record a session role: non-pair launches export a member credential, `ccb-role` accepts it, and `ccb-list` shows the role beside the provider without printing the member ID. Pair launches keep their existing live ID and token as the same credential.
- Role claims are serialized by a launch-scoped lock, so concurrent claims for one name produce exactly one holder, and a malformed live-session inventory never falls back to provider entries.
- Pi is a local `ask` caller: its requests reach the lead and the result returns to the Pi pane through `.pi-session`, with Pi named in completion notifications.
- Clarified that Gemini CLI was retired for individual accounts on 2026-06-18; enterprise accounts still work.
- Added the three CCB role skills (`ccb-lead`, `ccb-implementer`, `ccb-ratifier`) to Pi, OpenCode, Gemini and Hermes. Each provider copy is rendered from one provider-neutral body so the copies cannot drift; only the `ccb-*` role directories are written, and only for providers whose home already exists or whose override variable is set.
- Added duplicate Codex pairing, including coexistence with other unique providers, with contextual sibling routing from either Codex pane.
- Added exact live-session identity through queueing, receipts, completion delivery, task retrieval, status, cleanup, and provider kill operations.

### Changed

- Flipped the default tmux status bar to a single-row `slim` layout: mode badge and git info on the left, project folder in the centre, live CCB request activity plus the user's `@ccb_status_right_extra` widgets on the right, with no agent name, version or clock. `status_bar = "classic"` in `ccb.config` (or `CCB_STATUS_BAR=classic`) restores the previous two-row layout; an unknown value warns and falls back to `slim` without failing the launch. `status-right-length` is 360 in both layouts. The activity segment shows running/queued CCB requests for the focused pane's project on a possibly shared daemon, counts CCB requests only (never typing into a pane), and is empty when idle or on error.
- Made collaboration roles session-assigned and provider-neutral in managed templates and distributed command guidance.
- Preserved unique-provider and cross-project peer workflows while making duplicate remote destinations fail explicitly as ambiguous.
- Kept native model, effort, account, resume, and subagent choices outside CCB; native subagents remain internal to their owning top-level session.

### Fixed

- Kept a launch's identity in the Claude session binding when the Claude daemon rewrites it, so a role claim still authenticates after that rewrite; a binding without a launch id no longer authenticates at all.
- A role assignment no longer survives an in-place respawn or a replacement pane. On a terminal that can respawn a pane in place (tmux), a claim stores the pane process that was validated when the role was recorded, and a pane whose process changed is not current; a replacement pane is refused by its pane id. Roles recorded by an earlier release have no such witness and are therefore not current after upgrading: re-run the role skill or `ccb-role set` once in each pane.
- `ask pi` no longer refuses to send when Pi's last reply mentions words such as "working" or "running". Pi is treated as busy only when its own activity indicator is shown (the spinner in the editor border, or a built-in status line above the editor), and the pane is checked twice before sending.
- `ask pi` now delivers the reply after Pi is switched to an earlier session inside Pi (for example with `/resume` after a relaunch). The request is located in any valid session of the project's Pi session folder, and a request that has not yet appeared is no longer reported as "not accepted" while Pi shows activity.
- Codex and Claude replies whose final line ends with the request's `CCB_DONE` marker inline now complete once the provider's turn has ended, instead of waiting out the idle timeout and failing as incomplete. The marker is stripped from the delivered reply.
- `ask` now prints a `[CCB_QUEUED ...]` line before the submit marker when the destination session is busy, and `pend` shows whether a pending task is running or how many tasks are ahead of it. The probe is capped at 0.25s and stays silent on any error.
- Prevented descendant Codex rollout logs with copied request anchors from winning destination reply selection.
- Kept WezTerm peer delivery valid when Claude or Codex replaces CCB's launch-time pane title.

## 0.13.1 - 2026-07-29

### Changed

- Restored transcript-plus-receipt `pend` history without leaking partial or foreign request turns.
- Validated peer sender routing through the host daemon so managed Codex cwd and sandbox boundaries cannot stamp an unusable reverse-reply target.
- Made Codex reply assembly and recovery select one marker-bearing final response instead of joining interim or historical finals.
- Bounded agent-visible replies at 64 KiB across foreground asks, background logs, completion callbacks, and `pend`, preserving oversized results as checksummed artifacts.
- Made exhausted WezTerm submission retries fail explicitly and notify the originating pane when detached peer delivery fails.
- Persisted peer delivery evidence as `observed`, `sent`, or `failed`; `pend` now reconciles late transcript anchors and labels unconfirmed sends without automatically retrying.
- Extended `ccb clean` to prune expired oversized-reply artifacts, including dry-run and all-project modes.

## 0.13.0 - 2026-07-21

### Changed

- Replaced the relay-style Claude-to-Codex review doctrine with symmetric mutual ratification: either collaborator may propose, verify, amend, contest, or implement work according to evidence rather than provider identity.
- Fixed global Codex guidance installation and migration so the managed collaboration block is written to `${CODEX_HOME:-~/.codex}/AGENTS.md` instead of the unused installation-directory `AGENTS.md`, with surgical uninstall support and shadowing warnings for `AGENTS.override.md`.
- Made implicit `pend` lookup strict to the current CCB tab, allowed paired panes to retrieve the same tab's tasks, and normalized `peer-<provider>` receipts by their responding provider.
- Added context-first exact task guidance plus `pend peer`, `pend local`, and newest-first task history with task-ID prefixes. `pend <provider> N` now reads task receipts; legacy provider conversation history requires explicit `--legacy`.
- Made pre-restart and otherwise unbound receipts exact-task-ID-only, with clear implicit-lookup guidance, while preserving the same-turn Async Guardrail.
- Made counted `pend` history skip in-flight tasks and backfill from completed replies, while single-task lookup retains pending-state polling.
- Prevented sandbox PID-namespace isolation from falsely marking fresh waiters incomplete by persisting task timeouts and requiring status staleness beyond the timeout plus grace period.
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
