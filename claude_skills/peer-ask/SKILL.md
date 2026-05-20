---
name: peer-ask
description: Send a message to Claude in another active CCB project using natural language. Use when the user says things like "Ask PRG Claude about X", "Tell content-automation Claude...", or "Ask [project] Claude...". Discovers active projects via ccb-list, matches by alias/name, asks for clarification if ambiguous, and remembers confirmed aliases.
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

```
Bash(CCB_CALLER=claude ask --peer "<work_dir>" "<message>")
```

Use the exact `work_dir` from `ccb-list --json` as the target. Pass the full message the user wanted to convey.

Follow the **Async Guardrail** in CLAUDE.md — if output contains `CCB_ASYNC_SUBMITTED`, end turn immediately.

### Step 5 — Report back

When the reply arrives (via `/pend` or completion hook), summarize what the other Claude said and continue the conversation.

## Examples

User: "Ask PRG Claude if the discount validation logic is thread-safe"
→ ccb-list → match "PRG" to product-review-generator → ask --peer

User: "Tell content-automation Claude we're using the new schema"
→ ccb-list → match "content-automation" → ask --peer

User: "Ask Claude in project 3 about the current task"
→ ccb-list → use index 3 directly → ask --peer

## Notes

- Only Claude panes are targeted (providers.claude). Other providers are not reachable via this skill.
- If the target Claude pane is stale (not alive), report the error and show the live options.
- The `--peer` flag accepts the full `work_dir` path — always use path form for reliability.
