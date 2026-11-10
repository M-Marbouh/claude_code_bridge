---
name: peer-ask
description: Send messages to Claude or Codex panes in another active CCB project when the user names another project, asks another project's provider, or requests cross-project messaging. Use ask for same-project providers; Gemini and OpenCode are not supported cross-project.
---

# Peer Ask — Cross-Project Provider Messaging

Send a message to Claude or Codex running in another active CCB project.

## When to Use

Trigger this skill when the user references another project's Claude or Codex:
- "Ask BSA Claude about X"
- "Ask BSA Codex to review X"
- "Tell web-frontend Claude to..."
- "Ask the api-tests Claude if..."
- "Send this to Codex in my other project"

## Execution Steps

### Step 0 — Reply to an inbound peer message

If the message you are responding to contains a line like:
```
CCB_REPLY_TARGET: /path/to/sender/project
```

use that path as the peer target. Do not run `ccb-list` or rediscover the sender unless the send fails or the user asks you to choose a different target.

Read `CCB_PEER_INTENT` and `CCB_REPLY_EXPECTED` in the message metadata:
- `notify` / `no`: consume the information and do not send a reverse peer message. Treat any embedded question as informational; the sender must use `background` or `wait` to request an answer.
- `wait` / `yes`: the sender is blocked; complete the requested work and reply promptly.
- `background` / `yes`: complete the work normally and reply when ready; the sender has continued other work.
- Missing metadata with `CCB_REPLY_TARGET`: treat as legacy `wait`.

**Rules for inbound peer messages — read carefully:**

1. **No acknowledgements.** Do NOT reply with "Got it", "Thanks for reaching out", "I'll look into that", or any variant. For reply-bearing requests, send only the completed result; notifications require no reverse message.

2. **Do the work first, then reply.** If the request requires research, review, analysis, or implementation, complete the currently assigned work before sending the reply. You may work directly, consult a mounted CCB session, or use native in-session agents under their product rules. CCB addresses only mounted top-level panes; native agents remain internal to their owning session and do not receive CCB routing identities.

3. **Reply with substance and matching intent.** The reverse peer message should contain the actual answer, findings, or outcome — not an acknowledgement. A terminal answer with no requested follow-up uses `--notify`. If your response asks the original sender a question or requests confirmation/action, use `--background` (or `--wait` only when blocked). Never place a reply-requiring question inside `--notify`.

Use `CCB_REPLY_PROVIDER` when present; otherwise use `claude`. Reply with:
```
Bash(CCB_CALLER=claude ask "<reply-provider>" --peer "/path/to/sender/project" --notify --reply-to "<CCB_PEER_TASK_ID>" <<'EOF'
<terminal-result>
EOF
)
```

If the result needs a follow-up answer, replace `--notify` with `--background`. Preserve `--reply-to` in either case.
For legacy inbound messages without `CCB_PEER_TASK_ID`, omit `--reply-to` rather than inventing an ID.
Do not add `--live-id` to a correlated reply: the receipt pins the return endpoint.

Do not add a local `CCB_DONE` for the inbound peer delivery. CCB treats peer delivery as complete once the message lands in your pane; the real answer is the reverse `ask --peer` message.

### Step 1 — Discover active projects

```
Bash(ccb-list --json)
```

Parse this fresh JSON array. Each entry has: `index`, `work_dir`, `ccb_project_id`, `providers`, and
a `sessions` array. Do not discard a project because a provider is absent from aggregate
`peer_providers`; inspect `sessions[].providers[provider]` when the aggregate is ambiguous or
incomplete.

If the user names Claude or Codex, require that provider. If the user did not name a provider, prefer
Claude for compatibility; if Claude is unavailable and Codex is the sole usable peer provider, select
Codex.

### Step 2 — Match the target project

The user's reference (e.g. "BSA", "web-frontend", "api-tests") should be matched against:
1. **Remembered alias** — check conversation memory or prior confirmed matches first
2. **Exact basename** — `basename(work_dir)` == reference
3. **Keyword/abbreviation** — reference words appear in `basename(work_dir)`, or reference is an abbreviation (e.g. "BSA" → "billing-service-api" because B-S-A are initial letters)
4. **Index** — if reference is a number, use `ccb-list` index directly

**If one clear match:** proceed to Step 3.

**If ambiguous or no match:** present the list to the user:
```
I found these active CCB projects:
  [1] billing-service-api  (17ba87da)
  [2] api-test-suite          (3ee7346a)
  [3] web-frontend        (b0e3e2c9)

Which project did you mean? (reply with number or name)
```
Wait for user selection before continuing.

After selecting the project, inspect `sessions[].providers[provider]`. A usable candidate has an exact
non-empty `live_id`, `alive: true`, and `mounted: true`. If exactly one usable candidate exists and
provider identity is unambiguous, keep the ordinary `ask <provider> --peer <work_dir>` form.

If multiple live sessions of the requested provider exist, never guess. Use a user-supplied exact
current `live_id`; or match a supplied `pane_id` or `live_slot` only within this project and provider,
require exactly one current match, and immediately convert it to that candidate's `live_id`.
`live_slot` is a current display position, not durable identity. If mapping is not unique and safe,
show concise candidates (`live_slot`, `pane_id`, `live_id`, mounted state) and ask which one. Do not
infer a session from provider, pane, slot, order, title, history, or aliases; a remembered alias may
identify only the project.

### Step 3 — Remember the alias

Once a target is confirmed (by match or user selection), save the alias for this session:
- If the user used a shorthand like "BSA", remember: "BSA" → `/path/to/billing-service-api`
- Use this in future turns without asking again

### Step 4 — Send the message

Choose intent before sending:
- `--wait` only when the current task cannot proceed without the answer.
- `--background` when a later answer is useful but current work can continue. This is the default choice for consultations and reviews during an active plan.
- `--notify` for FYI messages, status updates, and handoffs that require no answer.

```
Bash(CCB_CALLER=claude ask <provider> --peer "<work_dir>" --background <<'EOF'
<message>
EOF
)
```

Use the exact `work_dir` from the fresh `ccb-list --json` output and pass the full message the user
wanted to convey. For an explicitly selected session, add a discrete option:

```bash
Bash(CCB_CALLER=claude ask <provider> --peer "<work_dir>" --live-id "<live_id>" --background <<'EOF'
<message>
EOF
)
```

Never encode a selector in `work_dir` or message text. Omit `--live-id` when the usable provider
candidate is unique.

- For `--wait`, follow the Async Guardrail and end the turn when `CCB_ASYNC_SUBMITTED` appears.
- For `--background`, `CCB_BACKGROUND_SUBMITTED` means continue the current plan; do not stop or call `pend` immediately.
- For `--notify`, continue after delivery confirmation.
- `--notify` messages must not end with a direct question; the CLI rejects that contradictory intent.

### Step 5 — Report back

When a reverse peer result arrives, incorporate it into the active task. Background mode must not pause the plan while waiting.

## Another Machine

When the user names a machine together with a project ("ask server demo Claude about X"), the target is on another machine and is addressed as `--peer <host>::<project>`.

1. Run `ccb-list` (human output) and read the "Other machines" section: for each shared project it lists every provider's state, the exact session `live-id`s, and the reason when a provider is not usable. It is a **last observed** view, never live: run `ccb-list --remote` first when the listed age is older than about 10 minutes, the machine is `unknown (never checked)`, or the target project shows the provider absent or ambiguous. For machine-readable exact values use `ccb-list --remote-json`.
2. Match the machine to a configured host alias and the project to that host's project alias, case-insensitively. There must be exactly ONE match for each; use the canonical spelling shown. Zero or several matches, or a name that only resembles a local project: ask the user. Never guess, and never fall back to a same-machine project of a similar name.
3. Send with the canonical aliases:

```bash
ask <provider> --peer "<host>::<project>" --background <<'EOF'
<message>
EOF
```

Use `--wait` only when the current task cannot proceed without the answer and `--notify` for a one-way message. When the listed provider state is `ambiguous` or it shows more than one session, pass an exact `--live-id <live_id>` from that section, and ask the user when it is unclear which session they mean. Cross-machine targets support Claude and Codex only.
4. Constraints: one answer per request; a follow-up is a new request. `--reply-to` and `--foreground` are refused for cross-machine delivery, and `--live-id` cannot be combined with `--reply-to`.
5. Inbound message from another machine: it is labelled `CCB_REMOTE_*` and carries no local `CCB_REPLY_TARGET`. When a reply is expected it names `CCB_REMOTE_REPLY_COMMAND`; answer by running exactly that command from the pane that received it — never send a reverse `ask --peer`.

## Examples

User: "Ask BSA Claude if the discount validation logic is thread-safe"
→ if blocked on the answer: ccb-list → match → `ask claude --peer ... --wait`

User: "Ask BSA Codex to review the parser"
→ if other work can continue: ccb-list → match → `ask codex --peer ... --background`

User: "Tell web-frontend Claude we're using the new schema"
→ ccb-list → match "web-frontend" → ask --peer --notify

User: "Ask Claude in project 3 about the current task"
→ if other work can continue: ccb-list → use index 3 → ask --peer --background

## Notes

- Claude and Codex panes are supported. Gemini and OpenCode are not peer targets.
- If the requested provider is not mounted, report the error and show the live provider options.
- Multiple live sessions are an unresolved ambiguity, not an unconditional refusal: an exact current
  selection may use `--live-id`.
- The `--peer` flag accepts the full `work_dir` path — always use path form for reliability.
- Reply-bearing inbound messages include `CCB_REPLY_TARGET: <sender_work_dir>`. Use it as the direct reply path and choose `--notify` or `--background` according to whether your response requests a follow-up.
- Preserve `CCB_PEER_TASK_ID` as `--reply-to` so the response is correlated with the original consultation.
- Do not narrate transport internals (`anchor_seen`, `fallback_scan`, confirmation levels) unless delivery actually fails. On exit code 0, continue normally.
- Claude and Codex peer delivery is asynchronous and delivery-only. Both providers send replies with an
  explicit reverse `ask --peer` message; later local output is never forwarded automatically.
