<!-- CCB_CONFIG_START -->
## AI Collaboration

Use `/ask <provider>` to consult `codex`, `gemini`, `opencode`, or `claude` in the current CCB project.
Use `/cping <provider>` to check connectivity.
Use `/pend <task-id>` for an exact async result, or `/pend <provider>` for the current caller's latest task.

CCB uses one instance of each provider. Provider suffixes such as `:worker`, routing tags, abstract roles, and sub-agents are not supported.

## Async Guardrail (MANDATORY)

When `ask` outputs `[CCB_ASYNC_SUBMITTED`:
1. Reply with exactly one line: `<Provider> processing...`.
2. End the turn immediately.
3. Do not poll, sleep, call `pend`, inspect logs, or submit a duplicate request in the same turn.
4. Wait for the user or completion hook to deliver the result.

`[CCB_BACKGROUND_SUBMITTED]` is deliberately non-blocking: record the task ID and continue the current plan. A peer `--notify` delivery is one-way and requires no reply.

## Mutual Ratification

For substantive CCB work, Claude and Codex collaborate directly:
1. Claude proposes a claim, evidence, and intended action.
2. Codex verifies it and replies `concur`, `concur-with-amendment`, `contest`, or `insufficient-evidence`.
3. Codex implements only ratified work.
4. Claude validates the result; any requested fixes are proposals for Codex to accept, amend, contest, or send back for plan adjustment.
5. Material disagreement after one clarification round is escalated to the user.

Trivial edits keep the fast path. Ratification is tied to the evidence snapshot and must be repeated after material code drift.
<!-- CCB_CONFIG_END -->
