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

Provider and relational selectors resolve task receipts strictly within the current CCB tab:

```bash
pend codex
pend peer
pend local
pend codex 3
```

`peer` is relative to the current Claude or Codex pane; `local` is the provider in that pane. Provider lookup
normalizes ordinary and peer receipts, so `pend codex` can retrieve either `codex` or `peer-codex` tasks.
Both paired panes can retrieve receipts from their shared tab, while another same-project tab cannot win merely
because it has a newer task. `pend codex 3` prints the newest three completed matching replies with task IDs,
skipping in-flight tasks. Without a count, `pend codex` can still report the newest task as pending.

Models prefer an exact task ID already present in conversation context. Bare `pend` succeeds only when the
current tab has one unambiguous task. If the current session cannot be established, or a pre-restart receipt
belongs to an older session, implicit lookup fails with guidance to use the exact task ID.

Legacy conversation readers remain available temporarily:

```bash
pend codex --legacy 3
```

Legacy mode reads provider conversation history and is never selected merely because a numeric count is present.

## Runtime diagnostics

```bash
ccb-list
ccb-list --json
ccb-mounted
ccb-ping codex
```

`ccb-list` reports any project with a verified live provider, including Codex-only projects. Multiple
WezTerm tabs or tmux windows for the same project appear under one project entry as separate `sessions`;
the existing top-level `index`, `work_dir`, `ccb_project_id`, and `providers` fields remain available in
JSON output. `peer_providers` lists mounted Claude and Codex peer targets. The legacy
`peer_capable: true` field retains its original Claude-specific meaning. Use `ccb-list --stale` only when
historical or inactive records are needed for diagnostics.

Managed Codex sessions cannot access the host terminal-multiplexer socket or loopback TCP directly.
On Linux, CCB therefore records a private filesystem mailbox for each `askd` daemon under `/tmp`.
Sandboxed `ccb-list`, `ask`, `ccb-ping`, and `ccb-mounted` requests use that authenticated mailbox for
host-side discovery and runtime checks. Other clients try TCP first and automatically fall back to the
mailbox when loopback access is unavailable. Daemon discovery checks the inherited `CCB_RUN_DIR`, the
project-scoped path derived from the current working directory, and finally the legacy global path, so
Claude and Codex tool subprocesses remain functional even when they do not inherit CCB runtime variables.
Claude and Codex `ask --notify` delivery uses the same host path and remains one-way. Mailbox directories
are mode `0700`, request and response files are written atomically, and every request still requires the
daemon's random token. After upgrading an active CCB session, restart it once so its daemon state advertises
the mailbox; otherwise `ccb-list` and provider runtime checks report an explicit transport error instead of
incorrectly returning an empty list or claiming that a live pane is dead.

Project discovery retries a valid project daemon state before declaring it offline, avoiding transient
`daemon_offline` results during startup. Active registry entries must also belong to a live CCB launcher;
legacy `ai-<time>-<pid>` records derive that owner automatically, while `ccb-list --stale` retains dead-owner
history with the `launcher_dead` reason.

CCB waits up to 10 seconds for a newly launched `askd` to become reachable before warning, and reports an
early child-process failure immediately. Set `CCB_ASKD_START_TIMEOUT_S` to tune that readiness window.

`ccb-mounted` is a human diagnostics command. Delegation does not require a separate mounted skill: `ask` validates the provider session, pane, binding, and daemon before reporting successful async submission.

## Cross-project provider messaging

```bash
ask --peer ~/dev/another-project --wait "Answer needed before I continue"
ask --peer b0e3 --background "Review this while I continue locally"
ask --peer ~/dev/another-project --notify "FYI: deployment completed"
ask codex --peer ~/dev/codex-only-project --background "Review the parser"
```

Targets may be an exact path, a `ccb-list` index, or a project-hash prefix of at least four characters.
Plain `ask --peer` retains the historical `--wait` behavior. Background consultations do not trigger the end-turn guardrail, and notifications do not include a reply target.
Peer responses preserve the original task with `--reply-to <task-id>`. A notification is terminal and cannot end with a direct question; use `--background` when a follow-up answer is expected.
Claude and Codex targets both use delivery-only transport. The receiving provider sends any result with
an explicit reverse `ask --peer` message; CCB never captures a later local pane response as the peer reply.
Outbound requests from managed Codex use the same private daemon mailbox, so both target discovery and
message submission work without direct access to WezTerm, tmux, or a network socket.

Reply-bearing peer requests also store a task-correlated return receipt. Reverse replies first use normal
live-project discovery. If the sender has dropped out of `ccb-list`, CCB can fall back to the original pane
only after validating the expected provider, project path, live pane, exact pane CWD, and stored CCB pane
marker. CCB never routes through an unvalidated stale session. If the pane is genuinely unavailable, the
reply remains recoverable with `pend <original-task-id>` and the reverse command reports that delivery failed.

## Session safety

CCB groups runtime records by project path but routes requests using the concrete CCB session and caller pane whenever available. Implicit `pend` lookup is strict to that session; pre-restart receipts remain available by exact task ID. An unfinished task whose host PID is invisible inside a sandbox remains pending until its recorded timeout plus a short grace period has elapsed, preventing PID-namespace isolation from causing false incomplete results. Codex log binding is updated only after the target log contains the exact `CCB_REQ_ID` request anchor; a newer standalone Codex conversation in the same folder cannot win merely because it has a later timestamp.

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

- `0.13.0` — replaced relay-style review roles with symmetric mutual ratification and moved managed Codex guidance to the real global `${CODEX_HOME:-~/.codex}/AGENTS.md` target.
- `0.12.0` — returned to a single-instance architecture; retained Claude, Codex, Gemini, and OpenCode; added request-scoped task receipts and deterministic `pend`; removed worker/tag/role/sub-agent behavior; hardened same-folder Codex isolation.
- `0.11.x` — introduced runtime status, cleanup, and experimental multi-instance work. Multi-instance behavior was retired in `0.12.0`.
- `0.9.0` — added local project listing and cross-project Claude messaging.

This fork intentionally remains a local terminal tool, not a distributed orchestration platform.
