# Live provider pairing — execution plan

Status: Phases 1–4 committed through `b698bba`; Phase 5 in progress. This document is the execution source of truth until completion. It supersedes the June 2026 multi-instance plans for this feature.

Current execution authorization: complete the repository implementation, tests, public documentation, local commits and push to main. Disposable live tests must use Luna for Codex and Haiku for Claude. Do not modify maintainer-global rules or deploy over installed CCB. Those restrictions do not prevent an isolated test installation.

Baseline: `7afb75770e9298c0a58e8865a9d6fd7b3ff58106` (2026-09-07 inspection). Paths and symbol names below were checked against this baseline. Reconcile intervening changes before editing; do not restore historical files wholesale. Implementation authorization, commits, and release/deployment authorization remain separate from this plan.

## Problem and goals

CCB currently identifies one mounted session by provider and project. Users should also be able to launch `ccb codex codex`, with two independent top-level Codex panes, and address the other local Codex with `ask codex`. The normal `ccb codex claude` and `ccb claude codex` workflows remain natural and compatible. Roles follow explicit session instructions, never provider, launch order, model, or remembered history.

This is a public terminal product. A new user must be able to understand the feature from the README and CLI help without knowing this discussion or installing the maintainer's optional knowledge tools.

## Final identity and routing model (before Phase 1)

Keep the existing provider adapters and daemon transport. Separate these existing conflated concepts:

| Identity | Meaning and lifetime |
| --- | --- |
| Project | Canonical existing CCB project path/hash; unchanged. Shared knowledge and files belong here. |
| Launch | Existing CCB launcher identity/owner. Fresh on a fresh launch; do not infer it from newest project state. |
| Live session | New opaque identity for one provider process/pane within that launch. No user-chosen instance name or role. |
| Conversation binding | Provider conversation ID, canonical log path/root, and monotonically changing binding generation for that live session. |
| Task | Existing unique request/task ID plus exact originating and destination identities and binding evidence. |

Use additive session records in the existing launch registry, keyed by live-session ID, as the authoritative inventory for new launches. Each record includes provider, launch/owner, terminal backend and pane identity/marker, runtime/session-file path, active state, binding generation, native conversation ID and canonical log/root when known. Per-session runtime files may live under the existing launch runtime directory. Do not create another database or daemon. Add one small shared identity/resolution module (`lib/live_sessions.py`, proposed) rather than duplicate matching logic across commands.

Preserve legacy `providers` projections for unique providers and old record readers. For a duplicate provider, no compatibility projection may masquerade as a unique routable pane: expose an explicit ambiguous aggregate and the separate live-session records. Update all consumers before enabling duplicate launches. Legacy requests with no new identity fields keep existing unique-provider behavior and fail if the destination is no longer unique.

A provider chooses an adapter, not a role or unique destination. A pane number alone is not a conversation identity. A task destination is resolved once, before successful async acknowledgement, and subsequently validated, never selected again. Resolution may be performed by host-side preflight and returned to the client, using the existing authenticated RPC/mailbox. The host must validate supplied identities rather than trust arbitrary environment values.

### Local selection

1. Establish caller project/launch and verify the caller live session using registry, owner, terminal, and pane evidence.
2. A unique requested provider retains existing semantics.
3. With two Codex sessions in that launch, a caller verified as one selects the other. A shell, another provider, or an unknown caller cannot choose between them.
4. Keep dead/unavailable members identifiable until launch cleanup: loss of the sibling must report unavailable, never collapse into a self-ask.
5. More than one eligible destination is an error before task submission. Missing or conflicting caller evidence must not fall back to newest records, launch order, or history.

Required launch scope is two Codex occurrences, plus the existing unique providers. Reject a third Codex, repeated other providers, and duplicate-Codex `-r` explicitly before modifying panes. Do not accidentally advertise generalized duplicate-provider support. Roles are neutral for every existing provider; adding other duplicate adapters is optional follow-up.

### Peer selection

Peer ask is an independent addressing mode. Preserve project path/hash/list-index selection and all existing unique-destination behavior. An initial request to a project with multiple eligible sessions of the requested provider fails explicitly; there is no new destination syntax in this scope. A sender outside a pair cannot use its local identity to select the remote pair's "other" member.

Correlated replies use the original receipt's exact saved sender identity as their destination, through both daemon and direct fallback paths. Never prefer live project/provider resolution over that identity. Save the result before attempting delivery; unavailable/changed destinations leave it recoverable by exact task ID. Local `pend peer` is a relational history selector, not the cross-project `ask --peer` mode.

### Knowledge and role boundary

Share codebase-memory graph/index and project ADRs, Memsearch history, project documentation/checklists, working files, and standing rules. CCB must neither partition nor manage those systems. Receipts may also remain physically shared: identity and selection must be exact, not storage directories private. CCB routing must never consult recalled roles or ADRs to find a live destination.

Model, effort, authentication policy, role policy, and native subagents remain outside CCB. CCB must know each session's effective conversation log location. Independent account tooling must provide usable independent credentials where needed; changing globally shared credentials is not something CCB can make pane-local.

### Native execution boundary

CCB coordinates only top-level mounted sessions. Any native subagent workflow remains internal to whichever top-level session currently owns that work and must not affect provider-neutral routing, session identity, or role freedom. A session may implement/review directly or use its own native agents under native product rules. For example, Codex proposing, Claude challenging, Claude using internal agents, Claude synthesizing, and Codex reviewing is permitted when currently assigned, never prescribed by provider identity. Either member of a Codex pair may perform any of these top-level responsibilities.

CCB creates no native-subagent pane, registry identity, queue identity, role, or receipt sender identity. If an internal agent issues an ask, attribute it to the owning top-level live session only when that ownership is verified. Without verified ownership, reject the ask rather than guess, create an identity, or attribute it to the other paired session. Probed on the Claude side (see the evidence table): a foreground helper inherits caller identity identical to its owning pane, so ownership is already established by the same evidence a pane-issued ask carries, and a helper cannot be told apart from its owner. Per the Phase 1 boundary that indistinguishability is non-blocking; untested execution environments that withhold the evidence fail closed on their own. Alternatively the owning top-level session issues the ask itself. Replies return to that top-level session, not the internal agent. Concurrent internal asks use distinct task IDs and the existing top-level routing rules.

Excluding descendant/helper transcripts from candidate selection is transcript eligibility hygiene, not native-subagent management. CCB may inspect metadata to reject an ineligible transcript without registering or addressing its author. Preserve this filter even though native execution topology is outside CCB.

## Non-goals and compatibility contract

- No named workers, `provider:instance`, routing tags, role registry, model defaults, model tiers, automatic account switching, mandatory native subagents, or shared-memory isolation.
- No persistent duplicate-session resume slots across launches, automatic context transfer between paired conversations, or role broadcasting.
- Preserve single-provider `ccb -r`, supported terminal/platform behavior, mixed launch ordering/layout intent, current provider launch flags/environment, MCP/vault composition, daemon authentication/mailbox, and existing unique-provider pane reuse.
- Do not change `CCB_REQ_ID`/`CCB_DONE` meaning, async submission/notification semantics, receipt recovery, or oversized reply artifacts.
- Preserve old receipts for exact lookup. Missing identity in a legacy receipt is not permission to guess when duplicates exist.
- A fresh duplicate launch gets fresh live IDs. A second launch against already active records must not steal or overwrite sessions: retain existing unique reuse behavior; for an incompatible duplicate topology report a conflict rather than invent cross-launch adoption.
- Shared files remain shared; coordinating nonoverlapping edits and ADR writes remains agent workflow, not CCB locking policy.

## Native conversation changes: verified facts and conservative boundary

At baseline, `CodexAdapter.handle_task` sends to a pane and later searches for the exact request anchor. `_scan_latest_candidate_log` uses a process-wide `CODEX_SESSION_ROOT` (otherwise the default directory), project match, and request anchor. It returns the first matching candidate. `CodexLogReader` can bind an explicit path/ID. `CodexProjectSession.update_codex_log_binding` persists changes, also rewrites a resume command and can invoke automatic context transfer. Claude has `ClaudeProjectSession.update_claude_binding` and `resolve_claude_session`, but project/provider fallbacks are not proof of an idle pane's current conversation.

These mechanisms provide request evidence, but an anchor alone does not establish eligible destination identity or prove that an idle caller has not switched conversations. Peer-reported disk forensics on Codex CLI 0.153.4 found the same request anchor in top-level and descendant rollouts for the same cwd; an actual misread was not reproduced. The scanner's first-match mtime ordering and lack of source/parent eligibility checks were independently verified in source. This is a blocking destination-gate regression case, not merely caller-side work. Native resume can open an older log, so mtime-based stale thresholds cannot be a correctness criterion. Copied/resumed logs may contain historical anchors: require an eligible top-level conversation and unique current structured request observation, matching ID/root/project and a current read boundary, not any old anchor substring anywhere. Verify `id` versus `session_id` semantics: reported descendant records can share a root session_id while carrying another id. Do not assume their interchangeability. Observed source/parent metadata shapes are evidence for the probed version, not a universal provider contract.

Phase 1 has two gates: blocking destination/receipt identity, and an evidenced caller-delivery policy. Actively evaluate the narrowest safe condition preserving normal unchanged-session automatic delivery; save-only must not be selected early merely because it is easier. For both Codex and Claude, investigate in order: (1) native conversation/thread identity available in actual command execution context; (2) supported lifecycle signals associating start/resume/new-conversation with a specific pane; (3) log-based continuity conditions tested against older resume, sibling resume, unrelated new rollouts, alternate roots, and validation-to-send timing. Do not depend on Memsearch hooks, infer association from cwd alone, scrape titles for conversation IDs, or assume a CLI hook exists because another provider has it. Inspect installed provider code/documentation and run controlled probes before selecting a mechanism. No model-specific integration.

The suggested set of accounted-for top-level rollouts is an investigation candidate, not an approved continuity rule. Resuming an existing or sibling conversation may leave that set unchanged; an unrelated standalone conversation can change it without affecting the caller. A new rollout alone does not associate a conversation with a pane. Unknown metadata must not silently qualify a candidate. Audit initial discovery, explicit binding, stale rebinding and history readers, not only the scanner.

Fail-safe contract if evidence is missing:

- Binding state is unknown/stale/conflicted, never silently current. Do not fabricate a binding from the newest project log.
- Block operations that require an unproven sender/recipient binding with a clear diagnostic; do not acknowledge them as submitted.
- An already delivered request may be collected only from its uniquely proven request conversation. A switch must not cause queued tasks to migrate or an old task to update the new generation.
- Save completed results first. If the caller's current generation cannot be established, suppress unsolicited pane injection and preserve `pend <task-id>` recovery. Do not inject even a task-completed notice into an unverified replacement conversation.
- Use a verified switch event to invalidate the old generation before accepting more work. Bind the newly selected native conversation only with positive evidence; reject sibling ownership of the same root/conversation.
- If reliable observation is unavailable on a supported CLI/platform, report that limitation; do not claim transparent native switching there. A timestamp cache, manual acknowledgement, or one-time binding cannot prove that a later unobserved switch did not occur.

Destination isolation and exact receipt identity remain blocking and must not be weakened for convenience. Caller lifecycle detection need not block the entire feature when an evaluated conservative delivery policy safely suppresses uncertain injection. Save-first is universal; save-only with explicit task-ID retrieval is the final fallback after actively evaluating automatic delivery. Preserve automatic delivery wherever evidence reasonably establishes the intended caller context. Transparent detection beyond the proven envelope may be optional follow-up. Before Phase 2, document evidence sources, tested provider versions/platforms, ownership checks, invalidation conditions, blind spots, automatic-delivery conditions, fallback conditions and validation/send race limits in this plan. Include a positive unchanged-session automatic-delivery probe and switch/ambiguity negative controls; if no safe automatic condition is found, record the failed candidates and evidence rather than choosing save-only by default. These are investigation gates, not permission for a broad lifecycle framework. A solution requiring new public binding commands or unsupported instrumentation needs a bounded approved amendment. Receipt-only completion never substitutes for correct initial routing.

## Historical references

Read with `git show <commit> -- <paths>`; do not cherry-pick whole commits.

| Commit | Reusable lesson |
| --- | --- |
| `b5346c7` | Separate spawn occurrences from bare provider topology; isolate pane/runtime/session files and registry entries. |
| `49f5b43` | Explicit bound conversation identity; borrow isolation concepts only. |
| `3d63242` | Sibling conversation exclusion, exact binding, instance-aware kill, no latest-by-cwd guessing. |
| `2c061fb` | Removal of built-in worker/model policy is retained. |
| `f286f2f` | Preserve subsequent simplification, structured receipts, and request-anchor safety; do not reverse the retirement wholesale. |
| `2dd609e`, `e4c2397`, `37ee94b`, `892c7b3` | Preserve Codex peer support, validated host sender, sandbox delivery, and request-bound results. |
| `7fe3614`, `1ff8413` | Preserve launch composition and optional vault wrapping. |

Never restore automatic main-pane spawning, worker model/effort/sandbox defaults, tag routing, tier/account compound names, fixed architect/worker ownership, persistent duplicate resume, or old broad provider/session fallback logic.

## Phase 1 boundary (final, owner-set)

Blocking correctness invariants for this feature:

1. An ask reaches the intended sibling session and never self-routes.
2. The reply CCB consumes is the reply the intended recipient produced for that exact request.
3. Sender/recipient identity stays exact enough for receipts, correlated peer replies and task-ID retrieval to return to the correct originating session.

Anything that does not violate one of these is non-blocking and must not expand Phase 1. Specifically non-blocking: distinguishing a helper-originated ask from its owning session's own, and perfect observability of which conversation a pane is currently displaying. Where automatic delivery cannot be proven safe, fail closed and preserve retrieval by task ID.

The helper-transcript collision was a real violation of invariant 2 and is fixed and deployed (`ee5a5aa`). Phase 1 is closed. Reopen it only on new evidence of an actual violation of one of the three invariants, not on residual uncertainty.

## Phase 1 — binding feasibility and executable isolation contract (core gate)

**Goal:** pass the destination identity gate and establish an evidenced caller-delivery policy that actively preserves safe unchanged-session automatic delivery before choosing the final save-only fallback.

**Files/functions:** inspect `lib/askd/adapters/codex.py` (`handle_task`, `_scan_latest_candidate_log`, `_state_at_req_anchor`); `lib/codex_comm.py` (`CodexLogReader`, `_latest_log`, session-meta parsing); `lib/caskd_session.py` (`update_codex_log_binding`, `ensure_pane`); `lib/laskd_session.py` (`update_claude_binding`); `lib/claude_session_resolver.py` (`resolve_claude_session`); `lib/provider_log_reader.py` (`provider_log_reader`). Extend `test/test_codex_reply_phase.py`, `test/test_provider_log_reader.py`; add `test/test_live_sessions.py`. Record the chosen observation and exact new symbol(s) in this document before Phase 2.

**Minimal change:** build fixture-driven contract tests and narrowly scoped native CLI probes, not a launcher rewrite. Observe two distinct conversations, native resume to an older conversation, a new conversation in the same pane, an idle sender switch during a remote task, and separate log roots. Prove events identify the specific pane/session and when they become observable. Specify atomic validation/send limitations rather than claiming a race-free guarantee from a periodic scan.

Follow the ordered evidence investigation above for both providers. Probe native internal-agent asks to establish top-level ownership rather than assuming environment inheritance. Do not enroll internal agents into the live inventory. Add a destination fixture with the same anchor in an intended top-level rollout and a newer descendant rollout; reverse mtimes and require the same correct outcome. Reject multiple eligible top-level candidates, unknown/unproven metadata, and copied-context anchors. Check `id`/`session_id` exclusions against real metadata shapes. Source filtering supplements, never replaces, exact request and live-session evidence.

**Borrow:** exact anchor and sibling exclusion lessons from `3d63242`; current request-bound reply assembly.

**Do not restore:** old resume fallback or context transfer, model-policy probes/footers as identity proof.

**Compatibility risks:** undocumented provider lifecycle behavior and sandbox visibility. Probes must use disposable projects/conversations, never live user prompts or production data.

**Tests/acceptance:** `python -m pytest test/test_live_sessions.py test/test_codex_reply_phase.py test/test_provider_log_reader.py -q`. Negative controls: wrong project/root, shared native ID, stale marker, absent switch event, historical copied anchor, newer helper transcript, ambiguous eligible transcripts, and late output from the old generation must all fail the relevant signal. Capture probe command, provider version/platform, result, and smallest failure evidence in this plan. Require an unchanged-session positive automatic-delivery probe and switch/ambiguity negatives; record any unsupported case and evidence for final save-only fallback. For internal-agent asks, verified ownership yields the owning top-level sender/receipt and unchanged local/peer routing; missing or conflicting ownership rejects, with no new live identity. No broad suites or live writes are required merely to finish planning. Record both gate outcomes and the complete delivery policy before Phase 2; destination exactness cannot be waived by a caller-side fallback.

**Dependencies:** none. Blocking dependencies found here must be resolved before enabling duplicate mode; they are not optional work.

## Phase 2 — additive live inventory and deterministic resolver (core)

**Goal:** represent two sessions without changing unique-provider behavior.

**Files/functions:** proposed `lib/live_sessions.py`; `lib/pane_registry.py` (`_get_providers_map`, `load_registry_by_session_id`, `load_registry_by_pane`, `load_registry_by_project_id`, `upsert_registry`); `lib/ccb_runtime_status.py` (`iter_registry_provider_records`, `_select_records`, `_session_bound`, `provider_status_for_target`, status dataclasses); `lib/askd/adapters/base.py` (`ProviderRequest`); `test/test_live_sessions.py`, `test/test_registry_project_id.py`, `test/test_runtime_status.py`.

**Minimal change:** additive live-session inventory and immutable task route snapshot; legacy records adapt as unique sessions without rewriting user data. Shared resolver takes explicit mode (local/initial-peer/correlated-reply), scope and verified caller, and returns one concrete route or a structured ambiguity/unavailable/binding error. Use the existing registry write/locking approach, with host-owned atomic binding-generation updates; do not allow client paths or IDs to bypass validation. Reject duplicate native conversation ownership within the live inventory. Keep tombstones during a launch so lost siblings do not become self-targets.

**Borrow:** separate provider topology and instance inventory from `b5346c7`; use opaque IDs instead of qualified provider names.

**Do not restore:** public suffix parsing, configurable instance-policy maps, synthetic providers/adapters.

**Compatibility risks:** JSON consumers expecting `providers.codex.pane_id`, stale owner records, alternate same-project tabs. Preserve existing unique JSON fields; explicit ambiguity for duplicate projections; no newest-record selection for identity-aware operations.

**Tests/acceptance:** `python -m pytest test/test_live_sessions.py test/test_registry_project_id.py test/test_runtime_status.py -q`. Cover legacy record reads, unique-provider parity, both directions of the pair, shell/Claude ambiguity, other launch exclusion, sibling death, stale owner/pane reuse, malformed/spoofed identities, and same native conversation conflict. No launcher behavior changes yet.

**Dependencies:** Phase 1 identity/evidence contract.

## Phase 3 — exact route through ask, daemon, queues and bindings (core/local preservation)

**Goal:** submit once to an exact destination and collect only that conversation's response.

**Files/functions:** `bin/ask` (`main`, `_preflight_target`, `_send_via_unified_daemon`, caller inference, background command construction on POSIX/PowerShell); `lib/askd/daemon.py` (`_handle_request`, `_UnifiedWorkerPool.submit`); `lib/askd/adapters/base.py`; `lib/askd/adapters/codex.py` (`load_session`, `compute_session_key`, `handle_task`, candidate scanning); `lib/caskd_session.py` (`load_project_session`, `compute_session_key`, binding update); `lib/askd/adapters/claude.py` (`load_session`, `handle_task`, `_wait_for_delivery`, `_wait_for_response`); `lib/laskd_session.py`; `lib/claude_session_resolver.py`; `lib/askd_rpc.py` and `lib/askd_server.py` only where envelope validation/host operations require additive fields. Implement the proven Phase 1 binding observation in the exact code areas recorded there.

**Minimal change:** host preflight resolves and returns a route snapshot before async success. Persist it before launching the background waiter. Forward it unchanged; at enqueue and send validate that the destination is still the same live session/generation. Load the selected session in the worker and adapter instead of repeating provider lookup. Queue by live-session identity; tasks retain binding generation to reject stale queued work. Readers use per-session canonical roots and exact request/conversation evidence. Compare-and-update binding generation so an old task cannot overwrite a new binding. Do not run duplicate-mode binding through project-global auto-transfer/resume side effects. Preserve optional vault composition checks independently of identity.

**Borrow:** per-session worker isolation and anchor-confirmed binding, not old implementations that predate sandbox/receipt hardening.

**Do not restore:** latest-by-cwd or first-match selection, request redirection on unavailable destination, automatic resubmission of unconfirmed delivery, model/account policy.

**Compatibility risks:** preflight-to-send races, missing env in tool subprocesses, Windows async wrappers, shared daemon process roots, same conversation resumed in two panes. Model/effort changes alone must not change routing. Unknown/new account root is explicit binding failure; no scan of arbitrary account homes or credential copying.

**Tests/acceptance:** `python -m pytest test/test_ask_cli.py test/test_ask_client_flags.py test/test_askd_rpc.py test/test_daemon_only_cli.py test/test_codex_reply_phase.py test/test_provider_log_reader.py test/test_worker_pool.py test/test_live_sessions.py -q`. Two queue keys progress independently; same target serializes; target loss after preflight fails without reaching sibling; both TCP and mailbox preserve exact IDs; switched queued tasks reject; resumed older logs work with positive evidence; absent/duplicate anchors never collect unrelated output. Lifecycle observation is required by the Phase 1 gate. Use synthetic inventory until public launcher activation.

**Dependencies:** Phases 1–2.

## Phase 4 — receipts, completion and pend (local preservation)

**Goal:** exact history and delayed responses without cross-session delivery.

**Files/functions:** `lib/task_receipts.py` (`new_receipt`, `new_peer_receipt`, `find_receipt`, delivery updates); `lib/completion_hook.py` (`notify_completion`, `_run_hook_async`); `bin/ccb-completion-hook` (`main`, direct/fallback routing); `bin/pend` (`_current_session_context`, `_current_session_receipts`, `_resolve_responder`, `_overlay_items`, `_show_receipt`, `main`); `lib/provider_log_reader.py` (`provider_log_reader`, `provider_request_anchor_seen`); legacy `bin/cpend` and other provider readers only for ambiguity guards; `test/conftest.py` isolation of new environment fields.

**Minimal change:** add sender/destination live IDs, launch IDs, generations and proven conversation paths to shared receipts and completion payloads. Persist results before notification. Completion validates exact caller identity/generation and never re-runs contextual provider selection. Keep exact task lookup independent of live mount availability; history reads use the task's saved log, not the current pane's replacement log. Do not retroactively infer identity for ambiguous old receipts.

Unique-provider history/overlay behavior stays unchanged, including peer-provider receipt normalization. For duplicate mode, `pend codex` selects the other local Codex responder; `pend local` selects self; `pend peer` selects the sole other session in a two-session launch. Larger ambiguous relations fail. Restrict duplicate-mode implicit receipts to the exact relevant live identities; crossed A-to-B/B-to-A receipts must not merge. Exact task lookup remains the recovery path for cross-project requests and ended generations. Bare pend succeeds only with one unambiguous eligible task. Legacy history mode must reject duplicate ambiguity rather than read the default `.codex-session`.

**Borrow:** current receipt-first recovery and recent transcript overlay behavior; not provider-scoped historical instance readers.

**Do not restore:** receipt guessing by latest timestamp across panes, raw completion return by provider, direct pane-ID-only trust, hidden retries.

**Compatibility risks:** breaking tab-shared unique history, peer/local naming confusion, read-only/invisible host PIDs, pending tasks mistaken for completed. Keep existing timeout classification and reply artifact handling.

**Tests/acceptance:** `python -m pytest test/test_task_receipts.py test/test_pend_exchange_readers.py test/test_provider_log_reader.py test/test_reply_artifacts.py test/test_live_sessions.py -q`. Crossed asks, identical provider strings, late old-generation replies, removed sender, reused pane, unknown generation, exact old receipt retrieval, pending overlay/count semantics, legacy mode rejection. Negative control must prove no terminal send occurs on stale/unknown completion destination while result recovery still succeeds.

**Dependencies:** Phases 2–3.

## Phase 5 — preserve independent cross-project peer communication

**Goal:** unchanged unique peer UX and exact correlated return delivery with either sender in a duplicate pair.

**Files/functions:** `bin/ask` (`_sender_candidate_evidence`, `_resolve_sender_work_dir`, `_peer_bridge_cmd`, `_run_peer_bridge_*`, `_handle_peer_mode`); `bin/ccb-bridge-ask` (`_resolve_target`, `_correlated_reply_receipt`, `_validated_direct_reply_target`, `_send_to_daemon`, `_send_to_direct_reply_target`, `_persist_peer_delivery`, `main`, bridge lock key); `bin/ccb-list` (`_registry_sessions`, `_dedupe_sessions`, `_session_entries`); `lib/ccb_runtime_status.py` host status operations; `lib/task_receipts.py` peer receipts; `test/test_ccb_bridge_ask.py`, `test/test_task_receipts.py`, `test/test_ccb_list.py`, `test/test_ask_client_flags.py`.

**Minimal change:** retain public path/hash/index/provider parsing, delivery-only peer transport, explicit reverse replies, wait/background/notify behavior, and authenticated host mailbox. Sender validation enumerates exact live sessions, including sandbox status. Initial remote duplicate target reports ambiguity before delivery; no role/default-first inference. Resolve a unique remote destination once and forward its identity (not only its work_dir/provider). For correlated replies, validate the saved return route first and use that same route through normal daemon or validated direct fallback. Change provider-scoped bridge serialization only as necessary to avoid conflating independently addressed sessions. Never silently drop to project/provider when saved identity is invalid. Saved replies remain recoverable even when return delivery fails.

**Borrow:** current correlated receipts and validated fallback; retain `2dd609e`, `e4c2397`, `37ee94b`, `892c7b3` behavior.

**Do not restore:** Claude-only peer assumptions, remote context interpreted as local "other", new peer selector syntax, later local responses captured as peer replies, provider-first correlated reply precedence.

**Compatibility risks:** top-level list aggregates losing ambiguity, valid sandbox senders rejected because only one provider record was exposed, existing replies working only via fallback. Current peer destinations remain Claude/Codex; other destination providers are not added incidentally.

**Tests/acceptance:** `python -m pytest test/test_ccb_bridge_ask.py test/test_ccb_list.py test/test_task_receipts.py test/test_ask_client_flags.py test/test_runtime_status.py -q`. Preserve all existing unique peer cases. Add each duplicate sender to unique remote Claude/Codex and exact return; unique remote sender to duplicate destination ambiguity; correlated reply into duplicate origin succeeds only at original member; initial ambiguity remains even if launch order differs. Both direct and daemon paths reject dead/reused/switched sender and save reply. Test missing reply receipt, mismatched project/provider/correlation, unconfirmed-delivery recovery, no resend, subdirectory sender and multiple project windows.

**Dependencies:** Phases 2–4. Must pass before duplicate launch is publicly enabled.

## Phase 6 — launch two Codex panes and complete lifecycle consumers (core activation)

**Goal:** enable `ccb codex codex` only after routing consumers are ready.

**Files/functions:** `lib/ccb_start_config.py` (`normalize_provider_tokens`, `normalize_start_config_data`); `ccb` (`_parse_providers`, `_parse_providers_with_cmd`, `cmd_start`, `AILauncher.__init__`, `_provider_env_overrides`, `_provider_pane_id`, `_start_provider`, `_start_provider_wezterm`, `_start_codex_tmux`, `_start_codex_current_pane`, `_write_codex_session`, `_write_cend_registry`, `_sync_cend_registry`, `cleanup`, `cmd_kill`, `_verified_kill_target`); `lib/caskd_session.py` (`ensure_pane`); `lib/ccb_runtime_status.py`; `bin/ccb-list` status/human rendering; `bin/ccb-mounted.py`, `bin/ccb-ping`, `lib/ccb_cleanup.py`; session prune helpers reached by existing cleanup; `bin/autonew`/`bin/ctx-transfer` for ambiguity guards only. Retain `_compose_agent_shell`/`_compose_agent_argv` and vault wrapping.

**Minimal change:** keep bare provider topology for adapter setup, add ordered launch occurrences for pane spawning. Preserve two Codex tokens from CLI and config, with automatic live IDs. Separate pane maps, runtime files/FIFOs/PIDs/markers/session records; unique markers include launch/live identity. No model policy flags added. Register each pane and maintain distinct bindings through native resume according to Phase 1. Display generated labels (Codex 1/2) for diagnostics only. `ccb kill codex` covers all verified Codex sessions in its existing project scope; closing one is a terminal action. Keep daemon shutdown semantics. Clean removes only owned stale runtime records under existing scope/dry-run/age behavior and never native conversation logs or knowledge stores. Do not let ensure_pane respawn a different conversation automatically in duplicate mode; unavailable is safer than a hidden replacement.

**Borrow:** `b5346c7` resource separation and `3d63242` kill enumeration. Retain current backend layout machinery.

**Do not restore:** historical main-pane auto-add, persistent resume slots, pinned models, per-instance policy maps, generic N-instance rollout. Reject duplicate `-r` before creating/relabeling panes; keep unique `-r` untouched.

**Compatibility risks:** current-pane/anchor code, tmux and WezTerm differences, POSIX/PowerShell env quoting, CCB startup pane reuse, count lost by config normalization, provider-level launch options accidentally applied differently. Additive env identity must survive tool subprocesses; role is never an env routing key. Existing launch_env/launch_args remain provider-level defaults; this phase does not invent per-account configuration. Root changes require the verified binding mechanism and clear diagnostics when unavailable.

**Tests/acceptance:** `python -m pytest test/test_ccb_tmux_split.py test/test_tmux_respawn_pane.py test/test_wezterm_backend.py test/test_ccb_kill.py test/test_ccb_cleanup.py test/test_ccb_list.py test/test_ccb_mounted.py test/test_ccb_ping.py test/test_ccb_agent_composition.py test/test_ccb_mcpv_phase2.py test/test_session_file_override.py test/test_live_sessions.py -q`. Verify two panes and two records/FIFOs where used, distinct sender env, both ask directions, no pane overwrite; mixed launch orders and existing subsets unchanged; duplicate config parity; third/unsupported duplicate and duplicate `-r` rejected before mutation; sibling death no self-ask; kill/clean ownership and dry-run negatives. Validate native switch, separate log roots, and same-root independent conversations using the Phase 1 real-provider probes.

**Dependencies:** Phases 1–5. Internal earlier commits may test synthetic sessions; duplicate launch is not enabled before these pass.

## Phase 7 — role-neutral rules and managed templates

**Goal:** either provider can perform the current assigned work, without a role engine or duplicated standing policy.

**Files/areas:** `config/agents-md-ccb.md`, `config/claude-md-ccb.md`, `config/claude-md-ccb-route.md`; proposed shared source `config/collaboration-rules.md`; `install.sh` managed Claude/Codex Markdown generation around `CCB_CONFIG`/`MUTUAL_RATIFICATION` blocks; corresponding `install.ps1` generation; `test/test_installer_managed_markdown.py`. Update distributed command skills under `claude_skills/` and `codex_skills/` for ask, peer-ask, pend, ping/cping, autonew where their current instructions assume unique providers or fixed roles, including existing PowerShell variants. Enumerate applicable files with `git ls-files` before editing; do not migrate unrelated skills.

**Minimal change:** author common CCB collaboration rules once; installer renders that common source with thin provider-specific capability/command guidance into the established managed blocks. Preserve markers, unrelated user text and existing install paths. This is text composition, not a new role service. Provide short role instruction examples, manually supplied after fresh start or native resume. No role is inferred when unassigned. Keep project facts in one authoritative project rules body with thin provider entry references where already suitable; no mass project-file conversion by the public installer.

Required shared wording:

> The current explicit assignment for this session determines its role. It supersedes provider identity, launch order, recalled Memsearch history, and previous session roles. Historical role assignments are context only and must not establish current ownership. If no current assignment exists, do not infer one from history. Role assignment does not override standing engineering rules or grant additional action permissions.

> Session role assignments are operational context, not architectural decisions. Automatic history capture may record them, but they must not be promoted into standing rules or ADRs. Durable decisions may define provider-neutral, session-assigned roles; they must not assign lasting ownership to whichever agent or model currently performs a role.

Retain evidence-based mutual ratification, bounded briefs, independent verification, provenance disclosure, engineering standards and authorized-action limits; express participants as proposer/implementer/reviewer or current assignee. Remove mandatory Claude synthesis/push/deploy, Codex-only planning/review, Claude-only tests, pinned native-subagent implementation, and precedence clauses that entrench those assignments. Native subagents remain optional provider capabilities under their own product rules, not part of CCB architecture. Reset/session selection remains the user's choice. A role assignment alone never authorizes push/deploy.

Re-express rather than delete useful internal-agent guidance: the owning top-level session labels internal-agent-authored diffs and submits them to the assigned reviewing session, which cannot infer provenance; retain review of every such submitted diff with depth proportional to risk. The owner integrates and verifies internal work. Preserve actual native capability/constraint documentation without mandatory provider/model placement. Specifically replace the blanket "Do not use Claude sub-agents" prohibition and Codex-only implementation direction in `claude_skills/peer-ask/SKILL.md` with the top-level coordination boundary and current-assignment rule. Enumerate both distributed skill trees and PowerShell variants for equivalent wording before editing; only that particular conflict has been verified here. Render the new shared boundary through both managed template paths.

**Maintainer live migration (separate targeted edits, no public installer rewrite of hand-authored text):**

This is a separate rollout gate and separately reviewed change, not a prerequisite for public product/clean-install acceptance. The user has agreed its direction and scope; prepare concrete targeted diffs and execute only under applicable authorization, never as incidental template generation. Report public feature completion and maintainer rollout completion separately. These global edits affect other projects and are not performed by this plan update.

- `/home/musta/.codex/AGENTS.md`: Subagents/Git Claude ownership clauses; Mutual Ratification instance prohibition and provider ownership; all fixed Work Placement bullets/precedence.
- `/home/musta/.claude/CLAUDE.md`: managed AI Collaboration single-instance text, Mutual Ratification ownership, Work Placement; hand-authored Codex Delegation Defaults; qualify How I Work synthesis/delegation defaults by current assignment.
- Repository-local `AGENTS.md`: Role and deployment ownership, provider-specific proposer/reviewer phrasing, deployment prohibition justified solely by Claude identity. Preserve authorization limits and technical constraints.
- Repository-local `CLAUDE.md`: make deployment procedure shared authorized project knowledge, not automatic Claude responsibility. Keep project paths/key facts.
- Other projects: inventory and report exact conflicting clauses before targeted authorized edits. Do not assume these local ignored files are distributed or that changing only a template removes inherited global conflicts. Provider capability limitations that are real remain intact.

**Borrow:** current symmetric ratification and managed-block preservation behavior.

**Do not restore:** role-based routing tags, built-in role hierarchy, cost/model-based work placement, role ADRs, installer overwriting hand-authored user doctrine.

**Compatibility risks:** reinstall duplicates text, missing common source on Windows, old hand-authored precedence defeating new rules. Shared template changes must work for ordinary users without local absolute paths or optional MCP tools.

**Tests/acceptance:** `python -m pytest test/test_installer_managed_markdown.py -q`; POSIX and PowerShell managed output fixtures, idempotent reinstall, preserved custom text, no fixed provider/model ownership. Behavioral probes: same project rules with lead/implementer assignments in both mixed orders and duplicate Codex; conflicting recalled roles ignored; native resume plus renewed assignment; no current role inferred without assignment; no new push/deploy permission. Inspect resulting ADR/standing-rule diffs to ensure temporary assignments were not promoted. Do not treat string-presence tests as proof of agent behavior.

**Dependencies:** can prepare after Phase 2; final command guidance depends on Phases 4–6. Public feature acceptance requires distributed rule/template migration; maintainer global/project migration has its own execution and acceptance gate and remains an outstanding full-rollout deliverable until completed.

## Phase 8 — public documentation, help and release preparation

**Goal:** a new user discovers and operates the feature without internal context.

**Files/areas:** `README.md` existing Start a project, Ask and retrieve results, Runtime diagnostics, Cross-project provider messaging, Session safety, Configuration, Maintenance and Fork changes sections; `CHANGELOG.md`; `ccb` help/parser usage and the `VERSION` constant in `ccb` only at an authorized release; `bin/ask._usage`, `bin/pend.main`, `bin/ccb-bridge-ask.main`, `bin/ccb-list` help/status; distributed command skills from Phase 7. Use current README concise prose/examples and current changelog `## <version> - <date>` / `### Changed` style. Add an Unreleased section for pending work; do not invent a release date or version bump as part of implementation. Keep historical release entries accurate rather than rewriting history.

**Minimal change:** professional quick starts for `ccb codex codex`, `ccb codex claude`, `ccb claude codex`; explain assigning roles inside panes, either role on either provider, independent native model/effort/account choices subject to their tools, and no CCB model management. Show `ask codex` from within the pair and exact `pend <task-id>` recovery. Show existing peer syntax with unique remote destinations and explicit duplicate-remote ambiguity. Explain generated display labels are not instance-address syntax, unique compatibility, fresh launch IDs, unsupported duplicate `-r`, rejected third Codex/repeated other providers, and legacy history behavior. Give short lead and implementation role examples without provider/model/account assumptions.

Troubleshooting must distinguish: ambiguous target versus unavailable sibling; missing caller context; stale/unknown binding and supported native rebind workflow proven in Phase 1; account/log-root mismatch; completion saved but not injected; restart needed after upgrade. Do not promise account switch transparency beyond tested behavior. Document optional codebase-memory/Memsearch separately: shared project integrations, not core requirements, no CCB partitioning, no mandatory installation.

**Borrow:** current README organization, peer commands, diagnostic and recovery explanations.

**Do not restore:** June role/model diagrams/defaults or maintainer absolute-path quick starts; do not expose internal ID fields in the normal user workflow.

**Compatibility risks:** documentation claiming support before gates pass, outdated installed skill text, accidental change to plain `ask --peer` default or native `-r` contract.

**Tests/acceptance:** run `python ccb --help`, `python bin/ask --help`, `python bin/pend --help`, `python bin/ccb-bridge-ask --help`, `python bin/ccb-list --help`; verify examples in a disposable clean install on supported platforms. A fresh reader with no memory/graph integrations must launch a pair, assign different responsibilities, complete an ask/return, recover by task ID, use a unique cross-project peer, and understand an ambiguous remote error using public docs alone. No real account names or secrets in examples. Link check local README references and inspect changelog consistency.

**Dependencies:** Phases 4–7 and the proven Phase 1 support envelope.

## Phase 9 — integrated acceptance and release gate

**Goal:** prove shared knowledge, live isolation and public compatibility together.

**Files/areas:** existing `test/` suites above; new `test/test_live_sessions.py`; `.github/workflows/test.yml` and `.github/workflows/cross-platform-test.yml` only for adding necessary new coverage to their existing matrices; this document's evidence section. No new general test framework. Tests use `test/conftest.py` temporary HOME/TMPDIR isolation; never overwrite actual user registries, credentials or memory stores.

**Minimal change:** fill cross-phase test gaps, run existing suites, and perform real-provider/terminal acceptance separately from mocks. Tests should assert externally meaningful delivery/absence of delivery and receipt content, not mirror private implementation structure.

**Borrow:** existing terminal fakes, receipt fixtures, mailbox tests and isolated test home. No restored obsolete multi-instance suite wholesale.

**Do not restore:** forced model differences as proof of isolation, broad daemon kills, CCB-managed native-subagent orchestration, writes to shared knowledge solely for testing live user data. Native workflows internal to a mounted session remain permitted.

**Compatibility risks:** mocked success hiding terminal races, subdirectory namespace divergence, account roots working only under developer daemon env, Windows packaging differences. CI coverage is not a substitute for native switch evidence.

**Tests/acceptance:**

1. Run `python -m compileall -q lib bin ccb` and `python -m pytest test/ -q` after focused phases pass; retain existing CI platform matrix. Record pre-existing failures against the baseline rather than silently relaxing them.
2. Real tmux and WezTerm pair: bidirectional asks, concurrent requests with distinguishable sentinels, native fresh/resume into older logs, model/effort changes, separate account log roots, caller switch while waiting, killed/reused pane, stopped sibling, repeated launch. Every isolation canary has a wrong-target/anchor/root/generation negative control that cannot pass via a client-only error.
3. Mixed-provider launches in both orders, unique Gemini/OpenCode workflows, single-provider `-r`, optional vault wrapping, async guardrails, CLI config and status/kill/clean parity.
4. Cross-project unique peer sends from each duplicate member, saved exact reverse route, dead/switched-origin recovery; remote duplicate ambiguity before send; preserved notify/background/wait and sandbox mailbox behavior.
5. Optional integration acceptance in a disposable project with the maintainer's integrations: both agents resolve intended same codebase-memory project and ADR source, same Memsearch collection and shared docs; verify root and subdirectory launch. Inspected hook difference: Claude uses Git root, Codex hook uses supplied cwd, so mismatch must be reported as integration configuration, not fixed by CCB creating per-session knowledge stores. Resolve any required integration setup explicitly outside CCB before claiming that optional acceptance passed. Stock users do not need either tool. No claims of distributed concurrent-write guarantees from mere shared accessibility.
6. Role behavior: conflicting historical role recall cannot override new assignments; swap assignments without provider-file edits; unassigned sessions do not infer roles; role assignment does not grant push/deploy; automatic memory history may capture operational roles without ADR promotion.
7. Clean-install new-user walkthrough from Phase 8. Fresh launch never adopts previous duplicate histories; historical task IDs remain recoverable.
8. Native internal execution: exercise an assigned propose/challenge/internal-implementation/synthesis/review workflow without fixed provider ownership, plus direct implementation in a Codex pair. When an internal agent issues an ask with verified ownership, local contextual selection, peer sender validation, receipt IDs and completion destination remain those of the owning top-level session; no additional registry/pane identity appears. Missing/conflicting ownership fails explicitly. Descendant transcripts containing matching anchors remain ineligible regardless of mtime. Preserve labelled diff provenance and risk-scaled review without assuming or requiring any particular native model.

**Dependencies:** all public-product phase gates, including the evidenced Phase 1 delivery policy. An unproven destination signal blocks release. An unproven caller-continuity condition invokes the documented final fallback after the required automatic-delivery evaluation; report its support envelope, not transparent switching. Maintainer global/project migration is a separate rollout gate and not a prerequisite for public release. Do not push, deploy, or bump release metadata without the applicable authorization.

## Required versus optional work

Core pairing: Phases 1–3 and 6. Local/peer preservation: Phases 4–5, integrated gate 9. Distributed rules/templates: Phase 7. Public product documentation/release preparation: Phase 8. All are required for public feature completion. Phase 7 maintainer migration is a separate required full-rollout deliverable with separate execution, acceptance and reporting; it does not gate public product completion.

Optional future work, excluded now: explicit initial peer destination selector; more than two Codex sessions; duplicate Claude/Gemini/OpenCode; persistent CCB duplicate resume; automated per-pane role instruction delivery; account launch convenience; generalized lifecycle integrations beyond the narrowly proven requirement. None may be smuggled into core scope to make an ambiguous case appear successful.

## Execution discipline and evidence

### Implementation verification — 2026-09-08

Phases 1–4 are committed through `b698bba`. Phase 5 remains under integration:
peer sends with saved endpoints now use endpoint-keyed daemon queues and send
to the validated pane rather than reloading a provider default. Sender lookup
distinguishes legacy absence from present-empty/invalid inventories and fails
closed on host lookup errors. Listing uses explicit live-session binding files.

Focused peer/worker/list/client tests: 126 passed. Full host-permission suite:
629 passed, 1 skipped; compileall and diff whitespace checks passed. The initial
sandbox run could not create localhost sockets; the eight affected tests passed
with host permissions and isolated HOME/runtime directories. In-memory negative
controls reverting empty-inventory refusal and exact peer dispatch each failed
their targeted tests. No production panes or installed files were changed.

Still required before Phase 5 completion: audit saved peer identity across all
entry points and queue/send boundaries, listing identity/count semantics, and
receipt capture for inventory-only senders. Live-provider, platform, clean-install,
documentation, and role-template acceptance remain unperformed. Passing fixture
tests do not establish those gates.

Implement phases in dependency order. Each phase is a bounded implementation/review unit with focused tests; do not enable public duplicate launch until preservation phases pass. The implementing agent owns its diff and test evidence; the reviewer checks the exact resulting diff. Current user assignment determines who fills those responsibilities. No provider/model is prescribed.

Update this authoritative plan only for material implementation discoveries, the Phase 1 proven mechanism, and acceptance evidence; do not create mirrored handoff logs or ADRs recording daily assignees. A change to an invariant or public contract requires a fresh explicit design amendment. Do not weaken isolation to make a test pass.

| Gate | Evidence at plan creation |
| --- | --- |
| Baseline and file/function inventory | Inspected at the HEAD stated above. |
| Native switch observation | Non-blocking per the Phase 1 boundary: it affects where a correct reply is displayed, not which conversation produced it. Save-first universal; fail closed to task-ID retrieval where automatic delivery is unproven. |
| Helper transcript collision | RESOLVED. Reproduced with fixtures: descendant rollout won selection when newer, correct conversation won when older. Fixed in `ee5a5aa` by rejecting rollouts with positive lineage evidence (non-empty `parent_thread_id`, or `source` marked subagent); rollouts predating those fields stay eligible. Regression test asserts both mtime orderings and fails with the guard removed. Suite 405 passed, 1 skipped. Deployed. |
| Native internal asks | Probed on WezTerm, Claude side, foreground helper: the helper inherits `CCB_CALLER`, `CCB_SESSION_ID`, `CCB_RUN_DIR` and `WEZTERM_PANE` identical to its owning pane, so attribution to the owner is already correct and indistinguishable from a pane-issued ask. Codex-side sandboxed helpers untested; missing evidence there rejects the ask, which fails closed. Non-blocking per the Phase 1 boundary. |
| Peer addressing | Project selector + provider verified; no initial duplicate discriminator; receipt direct route exists but live-provider precedence must change. |
| Rule conflicts | Global and project files and installer template sources inspected; no migration performed. |
| Shared knowledge | Same-root collection helpers agree; inspected Claude/Codex hook root derivation differs for subdirectories. |
| Feature tests and real-provider probes | Not run for this plan; implementation not started. |

## Artifact placement

The repository uses ignored `plans/` for historical plans and ignores `docs/`. This plan deliberately makes only `plans/live-provider-pairing-plan.md` trackable through a narrow `.gitignore` exception. Other internal plans/docs stay ignored. The execution document is public and self-contained; maintainer-specific migration paths are clearly separated from public user requirements.
