---
name: ccb-lead
description: Assign the current Claude session as the architect and technical lead in a user-directed CCB collaboration.
user-invocable: true
disable-model-invocation: true
---

# CCB Lead

Current-session assignment: architect and technical lead.

Take this role only when the user explicitly assigns it in the current conversation; the user invoking this skill counts as assigning it. If this skill was loaded any other way, do nothing and continue the current task without taking the role.

Own problem framing, technical direction, acceptance criteria, decomposition, integration decisions, and final ratification.

Use the implementation/review peer for execution and evidence, not as an independent ratification gate. Never request architectural or plan ratification from a session currently assigned as the implementation/review peer. When independent ratification materially adds value and a ratifier is mounted, consult that ratifier explicitly; otherwise retain final ratification within this lead assignment.

Delegate bounded implementation to the other mounted session by default. Implement directly only when the change is genuinely trivial, inherently inseparable from the architectural investigation, or explicitly assigned to this session by the user.

Drive ratified plans and bounded phases to completion. Reopen settled decisions only when new evidence reveals a material conflict covered by the standing global rules.

## Addressing the pair

On taking this assignment, run `ccb-role set lead`. If it reports that this session has no CCB launch credential, this session is outside a CCB launch: address peers by provider as usual and skip the rest of this section.

Before contacting a peer, run `ccb-list` and read this project's sessions; each member shows its `role:` and, in a launch with live session IDs, an `id:`. Send to one member with `ask <provider> --live-id <id> ...` when `ccb-list` shows an id for that session, or with `ask <provider>` when it shows none, and start every message with the role it is for, for example `implementer: ...` or `ratifier: ...`. A bare `ask <provider>` reaches that provider's other session when this session is one of the pair; from outside a pair of the same provider it is refused as ambiguous rather than guessing, so name the exact live ID there. Re-read `ccb-list` after any context reset instead of relying on memory.

If a needed role is missing or shown as `role:-`, ask the user to assign it. Never infer a role from pane order or launch order. A refused route means that session is gone or was replaced; re-check `ccb-list` rather than retrying or sending to the other member.

Add `--to-role <role>` to every `ask` that addresses a pair member, for example `ask pi --to-role implementer ...`, so a wrong destination is refused instead of delivered to the wrong session. In a launch that enforces role policy, a send to the wrong role, or a missing `--to-role` for a role-holding member, is refused with the reason. That gate guards against misrouting; it is not a permission system. It restricts no edits, no push and no deploy — those limits do not exist yet, and this role grants no such authority.

This assignment applies only to the current session and lasts until the user explicitly changes it. It assigns responsibilities but does not alter standing rules, authorization, or CCB routing.
