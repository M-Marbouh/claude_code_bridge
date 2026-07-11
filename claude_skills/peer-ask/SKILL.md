---
name: peer-ask
description: Send a message to Claude in another active CCB project. Use ask for same-project Claude, Codex, Gemini, or OpenCode.
metadata:
  short-description: Natural-language cross-project Claude messaging
---

# Peer Ask — Cross-Project Claude Messaging

Send a message to Claude running in another active CCB project.

## When to Use

Trigger this skill when the user references another project's Claude:
- "Ask PRG Claude about X"
- "Tell content-automation Claude to..."
- "Ask the test-builder Claude if..."
- "Send this to Claude in my other project"

## Execution Steps

### Step 0 — Reply to an inbound peer message

If the message you are responding to contains a line like:
```
CCB_REPLY_TARGET: /path/to/sender/project
```

use that path as the peer target. Do not run `ccb-list` or rediscover the sender unless the send fails or the user asks you to choose a different target.

Read `CCB_PEER_INTENT` and `CCB_REPLY_EXPECTED` in the message metadata:
- `notify` / `no`: consume the information and do not send a reverse peer message.
- `wait` / `yes`: the sender is blocked; complete the requested work and reply promptly.
- `background` / `yes`: complete the work normally and reply when ready; the sender has continued other work.
- Missing metadata with `CCB_REPLY_TARGET`: treat as legacy `wait`.

**Rules for inbound peer messages — read carefully:**

1. **No acknowledgements.** Do NOT reply with "Got it", "Thanks for reaching out", "I'll look into that", or any variant. For reply-bearing requests, send only the completed result; notifications require no reverse message.

2. **Do the work first, then reply.** If the request requires research, code review, analysis, or implementation: complete it before sending the reply. Use direct tools and normal CCB delegation only:
   - **Codex** (`/ask codex`) for implementation, code changes, or detailed technical work
   - Local tools for codebase searches, file lookups, pattern scanning, and planning
   Do not use Claude sub-agents; CCB supports only the mounted provider panes.

3. **Reply with substance.** The reverse peer message should contain the actual answer, findings, or outcome — not a status update. If a task is genuinely too large for one turn, break it into a concrete first result plus explicit follow-up questions.

Reply with:
```
Bash(CCB_CALLER=claude ask --peer "/path/to/sender/project" --notify "<result>")
```

The reverse result is a notification, so it must not make this Claude wait for another reply.

Do not add a local `CCB_DONE` for the inbound peer delivery. CCB treats peer delivery as complete once the message lands in your pane; the real answer is the reverse `ask --peer` message.

### Step 1 — Discover active projects

```
Bash(ccb-list --json)
```

Parse the JSON array. Each entry has: `index`, `work_dir`, `ccb_project_id`, `providers.claude.alive`.

Filter to entries where `providers.claude.alive == true`.

### Step 2 — Match the target project

The user's reference (e.g. "PRG", "content-automation", "test-builder") should be matched against:
1. **Remembered alias** — check conversation memory or prior confirmed matches first
2. **Exact basename** — `basename(work_dir)` == reference
3. **Keyword/abbreviation** — reference words appear in `basename(work_dir)`, or reference is an abbreviation (e.g. "PRG" → "product-review-generator" because P-R-G are initial letters)
4. **Index** — if reference is a number, use `ccb-list` index directly

**If one clear match:** proceed to Step 3.

**If ambiguous or no match:** present the list to the user:
```
I found these active CCB projects:
  [1] product-review-generator  (17ba87da)
  [2] test-builder-pro          (3ee7346a)
  [3] content-automation        (b0e3e2c9)

Which project did you mean? (reply with number or name)
```
Wait for user selection before continuing.

### Step 3 — Remember the alias

Once a target is confirmed (by match or user selection), save the alias for this session:
- If the user used a shorthand like "PRG", remember: "PRG" → `/path/to/product-review-generator`
- Use this in future turns without asking again

### Step 4 — Send the message

Choose intent before sending:
- `--wait` only when the current task cannot proceed without the answer.
- `--background` when a later answer is useful but current work can continue. This is the default choice for consultations and reviews during an active plan.
- `--notify` for FYI messages, status updates, and handoffs that require no answer.

```
Bash(CCB_CALLER=claude ask --peer "<work_dir>" --background "<message>")
```

Use the exact `work_dir` from `ccb-list --json` as the target. Pass the full message the user wanted to convey.

- For `--wait`, follow the Async Guardrail and end the turn when `CCB_ASYNC_SUBMITTED` appears.
- For `--background`, `CCB_BACKGROUND_SUBMITTED` means continue the current plan; do not stop or call `pend` immediately.
- For `--notify`, continue after delivery confirmation.

### Step 5 — Report back

When a reverse peer result arrives, incorporate it into the active task. Background mode must not pause the plan while waiting.

## Examples

User: "Ask PRG Claude if the discount validation logic is thread-safe"
→ if blocked on the answer: ccb-list → match → ask --peer --wait

User: "Tell content-automation Claude we're using the new schema"
→ ccb-list → match "content-automation" → ask --peer --notify

User: "Ask Claude in project 3 about the current task"
→ if other work can continue: ccb-list → use index 3 → ask --peer --background

## Notes

- Only Claude panes are targeted (providers.claude). Other providers are not reachable via this skill.
- If the target Claude pane is stale (not alive), report the error and show the live options.
- The `--peer` flag accepts the full `work_dir` path — always use path form for reliability.
- Reply-bearing inbound messages include `CCB_REPLY_TARGET: <sender_work_dir>`. Use it as the direct reply path and send the result with `--notify`.
- Peer delivery is asynchronous: receiving Claude should reply by sending a reverse peer message, not by trying to complete the original delivery request locally.
