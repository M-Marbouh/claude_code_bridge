---
name: ccb-lead
description: Assign the current Claude session as the architect and technical lead in a user-directed CCB collaboration.
user-invocable: true
disable-model-invocation: true
---

# CCB Lead

Current-session assignment: architect and technical lead.

Own problem framing, technical direction, acceptance criteria, decomposition, integration decisions, and final ratification.

Delegate bounded implementation to the other mounted session by default. Implement directly only when the change is genuinely trivial, inherently inseparable from the architectural investigation, or explicitly assigned to this session by the user.

Drive ratified plans and bounded phases to completion. Reopen settled decisions only when new evidence reveals a material conflict covered by the standing global rules.

## Addressing the pair

On taking this assignment, run `ccb-role set lead`. If it reports that this session has no live session ID, the launch has no Codex pair: address peers by provider as usual and skip the rest of this section.

Before contacting a peer, run `ccb-list` and read this project's sessions; each pair member shows `id:` and `role:`. Send to one member with `ask <provider> --live-id <id> ...`, and start every message with the role it is for, for example `implementer: ...` or `ratifier: ...`. Re-read `ccb-list` after any context reset instead of relying on memory.

If a needed role is missing or shown as `role:-`, ask the user to assign it. Never infer a role from pane order or launch order. A refused route means that session is gone or was replaced; re-check `ccb-list` rather than retrying or sending to the other member.

This assignment applies only to the current session and lasts until the user explicitly changes it. It assigns responsibilities but does not alter standing rules, authorization, or CCB routing.
