# Claude Code Bridge Fork

Lightweight, single-machine coordination for Claude, Codex, Gemini, and OpenCode.

This fork follows the upstream v5 terminal-pane architecture while keeping a deliberately small operating model:

- One top-level session per provider, with up to two Codex sessions alongside other unique providers
- No workers, provider suffixes, provider-bound roles, or CCB-managed subagents
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

Available providers are `claude`, `codex`, `gemini`, `opencode`, `hermes`, and `pi`. Gemini CLI was retired for individual accounts on 2026-06-18; enterprise accounts still work. A project may run any subset:

```bash
ccb codex claude gemini opencode
```

Pi is opt-in and is not added to the default provider list. Pi runs tools without approval; this is Pi behavior and is not enabled by `--no-approve`. CCB passes `--no-approve` to make Pi skip protected project resources; it does not provide a sandbox. CCB does not support in-pane `/tree`, `/new`, or `/resume` navigation while it manages the pane, because those actions can change the active branch or session and invalidate exact-request recovery.

Starting another CCB session for the same directory reuses the existing provider pane when possible and otherwise fails clearly. Qualified names such as `codex:worker` are rejected.

To open two independent Codex sessions:

```bash
ccb codex codex
```

A Codex pair may coexist with other unique providers, for example:

```bash
ccb codex codex claude
```

Inside either Codex pane, `ask codex` means the other verified Codex session in that launch. CCB assigns neither pane a role: give each session a short current assignment such as “lead and synthesize this task” or “implement and report evidence.” Either provider in `ccb codex claude` or `ccb claude codex` may take either assignment. Provider order is layout, not authority.

From the third provider in such a launch (for example Claude leading a Codex pair), `ask codex` alone is ambiguous and is refused. `ccb-list` shows each pair member's live ID; send to one exact member with:

```bash
ask codex --live-id <id> "implementer: build the parser"
```

A session in any launch may record the role its conversation was given with `ccb-role set <role>` (and `ccb-role clear`), and `ccb-list` then shows it for that provider, so a lead that lost its context can find who is who. In a launch with a Codex pair the role is shown next to the live ID; other launches show the role without printing a member ID, because that ID is not usable with `--live-id`. A role never selects a destination: `ask` chooses its destination by provider or route, and `--to-role <role>` only asserts that destination's role, which is verified on every launch; an enforcing launch also gates delivery on the role matrix below. A role grants nothing else. Only the session itself can record its role with `ccb-role` — with the one exception that a launcher records the roles configured in `ccb.config` at launch, using each session's own credential. A role already held by another member is refused, and a role disappears when the launch ends or the member's pane dies or is replaced. A member that starts a new conversation in the same pane keeps its displayed role, so tasks should name the role they are for and a member should decline a role it does not hold. `ccb-role show` reads a session's own role back: the role alone on stdout with exit 0, otherwise nothing on stdout, a reason on stderr and exit 1 (no role, another session's credential, a replaced pane, or a launch that is gone).

**Preflight destination-launch guarantee.** When the destination launch is ambiguous at preflight, CCB refuses with zero delivery instead of routing into another live launch. A session's own launch is identified by the member credential CCB exports into its pane (`CCB_MEMBER_ID`/`CCB_MEMBER_TOKEN`, with `CCB_SESSION_ID` naming the launch); pane evidence is only used inside that launch. A credential that is missing, wrong, contradictory, from another project, or names an inactive member or a positively dead launch is refused outright. When the credential is this session's but its binding cannot be confirmed right now — a WezTerm pane whose title marker does not resolve, a pane process that cannot be read, a stale binding, or a launch owner whose liveness cannot be proven — the route is demoted to a guarded fallback: it is allowed only when the credential-identified launch is the sole eligible launch for the project, and refused otherwise. If no credential authenticates the caller's launch — `ask` typed by hand outside a CCB launch, or from a managed sandbox where the daemon cannot receive the member credential — and the project has more than one live launch, `ask` refuses rather than guessing. **Known limitation — duplicate launches in one project:** that refusal is the compatibility exception for running two live CCB launches of the same project: unauthenticated `ask`/`--notify` in that situation now fails instead of picking one. The guarantee covers preflight only: a launch that starts, or a session binding that is replaced, after the preflight check is not covered, and pinning a destination through the queue and injection remains backlog.

Model, reasoning effort, account, and native subagent choices remain native tool settings. They may differ between panes and may be changed independently; CCB does not select or persist that policy. A third Codex or a repeated non-Codex provider is rejected. Duplicate-provider `ccb -r` is not supported; start a fresh pair and use each tool's native session controls if needed. Existing unique-provider `ccb -r` behavior is unchanged.

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

`peer` is the sole other session in an unambiguous two-session launch; `local` is the current bound session. In a two-Codex pair, `pend codex` selects the sibling's replies. Provider lookup
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
The destination provider must be unique in the remote project. An initial peer request to a remote two-Codex project is rejected as ambiguous; local “other Codex” context never crosses project boundaries. Correlated replies use the exact saved return identity and are not re-resolved by provider.
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

Peer receipts distinguish transcript-observed delivery from successful terminal submission whose transcript
anchor has not appeared yet. `pend <task-id>` rechecks late anchors and reports `delivery=sent-unconfirmed`
when delivery cannot yet be observed. CCB does not automatically resend unconfirmed messages because doing
so without receiver-side idempotency could create real duplicates.

## Session safety

CCB groups runtime records by project path but routes requests using the concrete CCB session and caller pane whenever available. Implicit `pend` lookup is strict to that session; pre-restart receipts remain available by exact task ID. An unfinished task whose host PID is invisible inside a sandbox remains pending until its recorded timeout plus a short grace period has elapsed, preventing PID-namespace isolation from causing false incomplete results. Codex log binding is updated only after the target log contains the exact `CCB_REQ_ID` request anchor; a newer standalone Codex conversation in the same folder cannot win merely because it has a later timestamp.

Each duplicate launch receives fresh internal live identities and separate binding files. The recipient is selected once, then revalidated at queue and send boundaries; receipts and delayed completion retain exact sender and recipient identity. If CCB cannot safely confirm a completion destination after a native conversation change, the result remains saved and recoverable with `pend <task-id>` instead of being injected into an uncertain conversation.

## Configuration

### Personal overlay

An optional private skill overlay lives at `${CCB_OVERLAY_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/ccb/overlay}`. Mirror the installer source layout with `claude_skills/<name>/` and `codex_skills/<name>/` directories. The shell installer applies overlay skills after public skills: a matching name fully replaces the public skill, while a new name is added. The Windows installer (`install.ps1`) does not apply the overlay yet.

The project configuration lives at `.ccb/ccb.config`:

```text
codex,claude
```

The same-provider pair can also be declared as JSON:

```json
{"providers": ["codex", "codex"]}
```

Provider instance names and `instances` overrides are not supported. Existing `provider_instances` and `instances` keys are ignored during configuration normalization, and stale worker session files are not loaded.

### Role skills

CCB ships three role skills — `ccb-lead`, `ccb-implementer` and `ccb-ratifier` — from one provider-neutral body per role (`role_skills/<role>.md`). Any supported provider can hold any role. The Pi, OpenCode, Gemini and Hermes installers render `SKILL.md` from that shared body at install time, adding only the frontmatter that provider's loader understands; Claude and Codex install their tracked `claude_skills/` and `codex_skills/` files byte for byte, and the tests keep those bodies equal to the shared one.

| Provider | Installs to | Override | Invoke |
| --- | --- | --- | --- |
| Claude Code | `~/.claude/skills/<role>/SKILL.md` | fixed path | `/ccb-lead` |
| Codex | `${CODEX_HOME:-~/.codex}/skills/<role>/SKILL.md` | `CODEX_HOME` | `$ccb-lead` |
| Pi | `${PI_CODING_AGENT_DIR:-~/.pi/agent}/skills/<role>/SKILL.md` | `PI_CODING_AGENT_DIR` | `/skill:ccb-lead` |
| OpenCode | `${OPENCODE_CONFIG_DIR:-${XDG_CONFIG_HOME:-~/.config}/opencode}/skills/<role>/SKILL.md` | `OPENCODE_CONFIG_DIR`, `XDG_CONFIG_HOME` | name the skill in your request; the agent loads it through its `skill` tool |
| Gemini CLI | `${GEMINI_CLI_HOME:-~}/.gemini/skills/<role>/SKILL.md` | `GEMINI_CLI_HOME` | `/skills list` shows it; the agent activates it through `activate_skill` after a consent prompt |
| Hermes | `${HERMES_HOME:-~/.hermes$HERMES_DATA_DIR_SUFFIX}/skills/<role>/SKILL.md` | `HERMES_HOME`, `HERMES_DATA_DIR_SUFFIX` | `hermes --skills ccb-lead` preloads it; the agent can also load it through its skill tool |

The skip-unless-home-exists rule applies to those four providers only: each is skipped unless its home directory already exists or its override variable is set, so installing never creates a config directory for a tool you do not use. Claude and Codex install unconditionally as before. Only the three `ccb-*` directories are written in any provider; every other skill in the same directory is left untouched, and uninstall removes only the three role directories. Hermes is resolved the way Hermes itself resolves its home — `HERMES_HOME` when set and non-blank, otherwise the platform default with `HERMES_DATA_DIR_SUFFIX` appended — so a leftover `~/.hermes` is not used as a fallback when the resolved home is absent.

Each body takes the role when the user explicitly assigns it in the current conversation, when the user invokes that skill, or when `ccb-role brief` printed that body for this session, and does nothing if the file was loaded any other way. On the `brief` path only, a session whose recorded role is a different one keeps the recorded role; an explicit reassignment by the user is still `ccb-role set <role>`. Pi additionally sets `disable-model-invocation: true` in the generated frontmatter, so its copy is only reachable through `/skill:ccb-lead`. OpenCode, Gemini and Hermes have no equivalent frontmatter field, so their guard is that sentence together with the explicit user request.

`ccb-role brief` prints the instructions that match the role this session has recorded, taking exactly the same validation path as `ccb-role show`: no role, no launch credential, a no-longer-current pane or a launch that is gone all print nothing on stdout, a reason on stderr, and exit 1. On success, for `lead`, `ratifier` or `implementer`, it prints the body of `role_skills/ccb-<role>.md` beside the installed `ccb-role` and exits 0; a missing body for one of those three is an install error (exit 1, empty stdout). Any other role `ccb-role set` accepts, such as `scribe`, is a custom role: CCB ships no instructions for it and never reads a file for it, and `brief` naming the role and saying none are installed, exit 0 — `brief` never changes or clears the recorded role, so a custom role stays usable. The body path is resolved from the executable's own location, so `ccb-role` must be installed as the usual link into the install prefix rather than copied into a bin directory — the same restriction its `lib` import already has.

CCB manages global instruction blocks for Claude Code and Codex only. On Pi, OpenCode, Gemini CLI and Hermes, add this to that tool's own global instructions file: `If you are a top-level CCB session (CCB_SESSION_ID is set) — never a subagent, helper or native agent spawned inside a session, even though it inherits that environment — run ccb-role brief once at the start of each conversation. If it prints role instructions, follow them: a role CCB recorded for this session is the user's assignment. If it fails, take no role. An explicit assignment from the user later in the conversation overrides it.`

Known limitation: peer-to-peer messaging (`ask --peer`) stays limited to Claude and Codex, as sender and as destination; a Pi session can start a local `ask` and receive the result, and can record a role, but a Pi request cannot be sent with `--peer`. Recording a role needs a CCB launch credential, which CCB exports into every managed pane; a session started outside CCB has none and cannot record a role.

### Role policy

A launch can enforce which roles may send new work to which roles. It is off unless the launch opts in:

```bash
ccb --role-policy enforce
```

or, to make it the project default, `"role_policy": "enforce"` in a JSON `.ccb/ccb.config` (see the example under [Pre-assigning roles at launch](#pre-assigning-roles-at-launch)). The plain-text config format cannot carry it: a `role_policy` entry there is rejected as a malformed provider entry and the launch refuses, so switch that project's config to JSON first.

The flag wins over the config value. The launcher writes the choice into the launch record before any pane starts, and the policy of the **destination** launch governs: a launch without the setting behaves exactly as before.

With enforcement on, the fixed matrix is:

| Sender role | May address |
| --- | --- |
| `lead` | `implementer`, `ratifier` |
| `implementer` | `lead` |
| `ratifier` | `lead` |
| a session with no role | nobody, and receives nothing ("assign a role first") |
| external (manual, email, another launch, `--peer`, legacy sender, missing credential) | only a session holding `lead` |

`ask --to-role <role>` asserts the destination's role and is always verified, even in a non-enforcing launch; under enforcement it is required for role-holding members. A refusal names the sender role, the target role and the allowed targets, and the request is refused before it is queued — never reported as submitted. Verified correlated peer replies still reach the original requester whatever role it now holds.

### Pre-assigning roles at launch

A launch can start with its roles already recorded, so an enforcing launch is ready as soon as its panes exist. In a JSON `ccb.config`:

```json
{
  "providers": ["claude", "pi", "codex"],
  "role_policy": "enforce",
  "roles": {"claude": "lead", "codex": "ratifier", "pi": "implementer"}
}
```

In a plain-text `ccb.config`, a provider token may carry its role:

```text
claude=lead, codex=ratifier, pi=implementer
```

Both forms mean the same thing and are optional per session: a provider left out of `roles`, or a `null`/`""` list entry, stays unassigned. A provider that appears more than once takes a list in the launch's provider order — `"roles": {"codex": ["ratifier", "implementer"]}` — and per-session roles for a duplicate provider are supported only where CCB already has a per-session identity for it (today the Codex pair). A role naming a provider the config itself does not list (a typo guard — a config with no provider list means the default one of `codex`, `gemini`, `opencode` and `claude`), a role count that does not match that provider's occurrence count, a role assigned to two sessions, and a malformed annotation (`codex=`, `=lead`, `codex=a=b`, or a bad role name) are all config errors: the launch refuses before any pane starts. A role for a provider the config lists but this launch does not start — such as one a CLI provider subset omits — is skipped instead: the launch prints one line naming each skipped provider and its role, and the providers it does start keep theirs. If a pane cannot claim its role — for example a WezTerm pane whose title marker does not resolve within about two seconds — the launch names the pane and the refusal, continues as `DEGRADED`, and that session can still run `ccb-role set <role>`.

`roles` records launch roles only: it does not load role skills, and a recorded role is not instructions by itself. A session reads the instructions for its own recorded role with `ccb-role brief` and takes that role's responsibilities when its conversation says so. The managed CCB instruction blocks (Claude Code and Codex; see [Role skills](#role-skills) for the other harnesses) tell a top-level session — never a subagent or helper, even though one inherits the environment — to run `ccb-role brief` once at the start of each conversation, and a role printed there counts as the user's assignment for that session; if it prints nothing, the session takes no role, and a later explicit assignment from the user overrides it.

**Known limitation — WezTerm:** roles currently work on tmux only. On WezTerm a role is tied to the pane's title marker, and Claude Code and Pi replace their pane titles once they start, so a role claimed at launch (or later with `ccb-role set`) stops counting a moment later. Under `--role-policy enforce` that leaves the session unreachable. A fix is planned; until then, use tmux for role-enforced launches.

**This guards against misrouting, not against permissions.** Every pane, daemon and the launcher run as one OS user, so any process can rewrite the files the gate reads or type into another pane directly. What enforcement does guarantee is that every CCB ingress path applies the same rule and fails closed: local `ask`, `--notify`, `--peer`, the bridge's direct reply path, background submissions and email ingress. **Action limits are not provided**: enforcement does not restrict edits, push, deploy or any other external effect, and a role grants no such authority.

## Troubleshooting

- `ambiguous` means CCB cannot prove one destination. Run the request from a mounted member of the local pair, or choose a remote project with one session of that provider; CCB never guesses by launch order.
- `unavailable` means the identified sibling, pane, daemon, or conversation binding is no longer usable. Restart the affected fresh launch rather than expecting a duplicate-provider resume.
- `unknown_caller` means the command was not attributable to one mounted top-level session. Run it inside the intended CCB pane and restart sessions created before upgrading.
- If a native conversation or account switch changes the provider's log root, send a new request only after that pane has established a usable binding. CCB will not scan arbitrary account homes or copy credentials.
- If a reply was saved but not injected after a caller switch, use the task ID printed at submission: `pend <task-id>`.

Optional project-memory integrations such as Memsearch and codebase-memory are shared project facilities, not CCB requirements. CCB neither installs nor partitions them. Configure both providers to resolve the same project root—especially when launching from a subdirectory—if you want their memory namespaces to match. Recalled historical roles are context only; the current explicit session assignment controls operational ownership.

## Maintenance

```bash
ccb clean --dry-run
ccb clean
ccb kill
ccb version
```

`ccb clean` also removes oversized-reply artifacts older than `--older-than`; `--dry-run`,
`--project`, and `--all-projects` apply to both stale session records and reply artifacts.

## tmux status bar and status-right extension

CCB themes the tmux status bar for the session its launcher runs in. The default layout is `slim`: one row, with the mode badge (`INPUT`/`KEY`/`COPY`) and git info on the left, the project folder in the centre, and on the right the live CCB request activity followed by your own `@ccb_status_right_extra` widgets last. There is no agent name, version or clock.

Choose the layout in `.ccb/ccb.config`:

```json
{"providers": ["codex", "claude"], "status_bar": "classic"}
```

`classic` restores the previous two-row layout (agent, version, the same activity segment, the clock) and its key-hint row; only the old daemon-dots segment is replaced by the activity segment. `CCB_STATUS_BAR=slim` or `CCB_STATUS_BAR=classic` overrides the config value; an unknown value prints a warning and falls back to `slim` instead of failing the launch. A plain-text `ccb.config` uses `slim`. Both layouts set `status-right-length` to 360.

The activity segment looks like `X 1▶2` — provider letter, running requests, queued requests — and lists only providers with active work. It reads the askd queues, is scoped to the focused pane's project even when one daemon serves several projects, and prints nothing when everything is idle or on any error. It counts **CCB requests only**: work delivered through `ask`, dashboard submissions or the daemon appears; typing directly into a provider pane does not. Peer (`ask --peer`) requests are queued against a destination pane rather than a project, so they are not counted — not every `ask` appears here.

Add a widget to the right side from your own `tmux.conf`:

```tmux
set -g @ccb_status_right_extra ' #(your-widget) '
```

Native `#[...]` styles and `#()` commands work in the value. Escape literal percent signs as `%%`. The value renders at the end of the status line, after CCB's own segments (after the clock in `classic`), and config reloads apply without restarting the theme. The theme lasts for the lifetime of the launcher that owns it. `slim` leaves room for several widgets there; this pairs well with [agent-quota.wezterm](https://github.com/M-Marbouh/agent-quota.wezterm), whose quota command can be set as `@ccb_status_right_extra` (optional, not installed or required by CCB); its [compact layout](https://github.com/M-Marbouh/agent-quota.wezterm#compact-layout) and [hiding inactive providers](https://github.com/M-Marbouh/agent-quota.wezterm#hiding-inactive-providers) sections cover fitting it into the one-row bar.

## Fork changes

- `Unreleased` — added independent `ccb codex codex` pairing with contextual sibling routing, exact live-session receipts and completion delivery, preserved cross-project peer behavior, provider-neutral role guidance, and explicit ambiguity failures.
- `Unreleased` — added opt-in Pi pane launch and request/reply support through Pi's versioned session transcript.

- `0.13.1` — hardened peer routing and failure reporting, restored exact `pend` overlays, bounded agent-visible replies, persisted delivery evidence, and added explicit reply-artifact cleanup.
- `0.13.0` — replaced relay-style review roles with symmetric mutual ratification and moved managed Codex guidance to the real global `${CODEX_HOME:-~/.codex}/AGENTS.md` target.
- `0.12.0` — returned to a single-instance architecture; retained Claude, Codex, Gemini, and OpenCode; added request-scoped task receipts and deterministic `pend`; removed worker/tag/role/sub-agent behavior; hardened same-folder Codex isolation.
- `0.11.x` — introduced runtime status, cleanup, and experimental multi-instance work. Multi-instance behavior was retired in `0.12.0`.
- `0.9.0` — added local project listing and cross-project Claude messaging.

This fork intentionally remains a local terminal tool, not a distributed orchestration platform.
