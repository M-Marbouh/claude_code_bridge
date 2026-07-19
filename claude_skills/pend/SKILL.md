---
name: pend
description: View an exact or current-tab CCB task reply using contextual task, peer/local, or provider routing.
metadata:
  short-description: View latest AI provider reply
---

# Pend - View Task Reply

Prefer the exact task being discussed. Provider and peer/local lookups are strictly scoped to the current CCB tab.

## Context Resolution (MANDATORY)

1. If an earlier async submission or result in the conversation identifies the requested task ID, run `pend <task-id>`.
2. Do not use the `CCB_REQ_ID` that wraps the current inbound request unless the user explicitly asks for that task.
3. If the user asks for the peer or local model without an exact task ID, use `pend peer` or `pend local`.
4. If the user names a provider, use `pend <provider>`.
5. Bare `pend` is allowed only when context has no better selector; it fails rather than guessing when multiple current-tab tasks exist.

`peer` is relative to the bound pane: Claude's peer is Codex, and Codex's peer is Claude. `local` is the model in the bound pane.

## Async Guardrail (MANDATORY)

If an `ask` command returned `[CCB_ASYNC_SUBMITTED ...]` in the current turn, do not run `pend`, poll, sleep, inspect logs, or submit another request. End the turn as required by the global Async Guardrail. Context-first routing applies only on a later turn or after a delivered completion.

## Execution (MANDATORY)

```bash
pend $ARGUMENTS
```

## Examples

- `/pend 20260711-120000-001-99`
- `/pend peer`
- `/pend local`
- `/pend codex`
- `/pend codex 3` — latest three current-tab Codex task replies
- `/pend codex --legacy 3` — explicit provider conversation history
