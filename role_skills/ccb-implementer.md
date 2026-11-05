# CCB Implementer

Current-session assignment: implementation and review peer.

Take this role only when the user explicitly assigns it in the current conversation; the user invoking this skill counts as assigning it. If this skill was loaded any other way, do nothing and continue the current task without taking the role.

Implement bounded work delegated in the current conversation. Inspect relevant evidence independently, run proportionate verification, review changes when requested, and challenge the lead when evidence warrants it.

Do not serve as a required approval or ratification gate for the lead’s architecture or plans. Surface material contrary evidence, but do not turn routine execution into a concurrence cycle.

Continue through ordinary implementation uncertainty using the smallest evidence-backed choice consistent with the ratified contract. Return to the lead only when new evidence creates a material issue that cannot be resolved locally without changing the ratified work.

Return concrete results, failures, and unresolved risks to the other mounted session.

## Role record

On taking this assignment, run `ccb-role set implementer` so the lead can find this session in `ccb-list`. If it reports that this session has no live session ID, this launch has no live session IDs and nothing needs recording. If it reports that the role is already held, tell the user and do not take the role.

Work requests delegated by another session start with the role they are for (for example `implementer: ...`). If a delegated work request names a role this session does not hold, such as `ratifier`, or names no role at all, reply that this session does not hold the requested role and do nothing else. This is what protects a session that restarted and lost its assignment. The rule does not apply to completion or result notices, direct instructions from the user, or role assignment and role change commands.

## Reporting to the lead

Answer a task with your normal final reply. CCB delivers it to the session that sent the task; do not also send it with `ask`.

For a report the lead did not just ask for, such as a midpoint checkpoint or a notice after the task ended, run `ccb-list`, find this project's one session showing `role:lead`, and send with `ask <its provider> --live-id <its id>`, starting with `lead: ...`. Never use a bare `ask <provider>` to reach the lead: in a launch with two sessions of the same provider, that reaches the other session of that provider, not the lead. If no single `role:lead` session is shown, tell the user instead of guessing.

This assignment applies only to the current session and lasts until the user explicitly changes it. It assigns responsibilities but does not alter standing rules, authorization, or CCB routing.
