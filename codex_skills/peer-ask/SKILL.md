---
name: peer-ask
description: Send messages to Claude or Codex panes in another active CCB project when the user names another project, asks another project's provider, or requests cross-project messaging. Use ask for same-project providers; Gemini and OpenCode are not supported cross-project.
---

# Peer Ask — Cross-Project Provider Messaging

Send a request to a mounted Claude or Codex pane in another active CCB project.

## When to Use

Use this skill for requests such as:
- "Ask BSA Claude about X"
- "Ask BSA Codex to review X"
- "Tell web-frontend Claude..."
- "Send this to Codex in my other project"

Do not use this skill for a provider in the current project; use `ask` instead.

## Inbound Peer Requests

Read the intent metadata:
- `notify` / `CCB_REPLY_EXPECTED: no`: consume the information and do not send a reverse peer message.
- `wait` / `CCB_REPLY_EXPECTED: yes`: the sender is blocked; complete the work and send the result promptly.
- `background` / `CCB_REPLY_EXPECTED: yes`: complete the work and send the result; the sender continued other work.

For a reply-bearing request, use `CCB_REPLY_TARGET` directly and use `CCB_REPLY_PROVIDER` when present
(otherwise default to `claude`). Complete the work first, then send a substantive explicit reply:

```bash
CCB_CALLER=codex ask "<reply-provider>" --peer "<CCB_REPLY_TARGET>" --notify --reply-to "<CCB_PEER_TASK_ID>" <<'EOF'
<terminal result>
EOF
```

Use `--background` instead of `--notify` when the response asks a follow-up question or requests
confirmation. If `CCB_PEER_TASK_ID` is missing, omit `--reply-to` rather than inventing an ID. Never
add `--live-id` to a correlated reply: the receipt pins its return endpoint. Do not produce a local
`CCB_DONE`; peer delivery is complete once the message reaches this pane.

## Send a Peer Request

### 1. Discover targets

Run:

```bash
ccb-list --json
```

Parse the fresh JSON array. Each project exposes `work_dir`, `ccb_project_id`, `providers`, and a
`sessions` array. Do not discard a project because a provider is absent from aggregate
`peer_providers`; inspect `sessions[].providers[provider]` when the aggregate is ambiguous or
incomplete.

If the user names Claude or Codex, require that provider. If no provider is named, prefer Claude for
backward compatibility; when Claude is unavailable and Codex is the sole peer provider, select Codex.

Match projects by remembered alias, exact directory basename, clear abbreviation, or list index. Ask
the user only when multiple projects remain plausible.

Within the selected project, inspect the requested provider's session offers. A usable candidate has
an exact non-empty `live_id`, `alive: true`, and `mounted: true`. Preserve the existing unique-provider
behavior: when exactly one usable candidate exists, send without a selector. When several live
sessions of that provider exist, never guess. Use a user-supplied exact current `live_id`; or match a
user-supplied `pane_id` or `live_slot` only within this project and provider, require exactly one
current match, and immediately use that candidate's `live_id`. A `live_slot` is only a current display
position, not durable identity.

If no candidate can be mapped uniquely and safely, show concise candidates (`live_slot`, `pane_id`,
`live_id`, mounted state) and ask which one. Do not infer a session from provider, pane, slot, order,
title, history, or aliases; a remembered alias may identify only the project.

### 2. Choose intent

- `--wait`: the current task cannot proceed without the peer result.
- `--background`: the result is useful later and current work can continue.
- `--notify`: one-way status or handoff with no answer expected.

### 3. Send

```bash
CCB_CALLER=codex ask <provider> --peer "<work_dir>" --background <<'EOF'
<message>
EOF
```

Use the exact `work_dir` returned by `ccb-list`. For an explicitly selected session, pass its identity
as a discrete option:

```bash
CCB_CALLER=codex ask <provider> --peer "<work_dir>" --live-id "<live_id>" --background <<'EOF'
<message>
EOF
```

Never encode a selector in `work_dir` or message text. If the session is uniquely usable, omit
`--live-id` for compatibility.

- `CCB_ASYNC_SUBMITTED` for `--wait`: end the turn immediately.
- `CCB_BACKGROUND_SUBMITTED`: continue current work; do not poll immediately.
- `CCB_NOTIFY_SUBMITTED`: continue after submission.
- A `--notify` message must not end with a direct question.

## Another Machine

A machine named together with a project ("ask server demo Codex to review X") is another machine: address it as `--peer <host>::<project>`.

1. Run `ccb-list` (human output) and read the "Other machines" section: per shared project it lists each provider's state, the exact session `live-id`s, and the reason when a provider is not usable. It is a **last observed** view, never live. Refresh it with `ccb-list --remote` when it is older than about 10 minutes, the machine is `unknown (never checked)`, or the target project shows the provider absent or ambiguous. `ccb-list --remote-json` gives the same values machine-readably.
2. Match the machine to a configured host alias and the project to that host's project alias, case-insensitively. Exactly ONE match is required for each; use the canonical spelling shown. Ask the user when there is no match or more than one — never guess, and never fall back to a same-machine project with a similar name.
3. Send with those canonical aliases:

```bash
CCB_CALLER=codex ask <provider> --peer "<host>::<project>" --background <<'EOF'
<message>
EOF
```

`--wait` blocks for the single answer; `--notify` sends one-way. When the section reports `ambiguous` or lists more than one session for that provider, pass an exact `--live-id <live_id>` and ask the user if it is unclear which session they mean. Only Claude and Codex can be targets.
4. One answer per request; follow-ups are new requests. `--reply-to` and `--foreground` are refused for cross-machine delivery.
5. An inbound message from another machine is labelled `CCB_REMOTE_*` and has no local `CCB_REPLY_TARGET`. When a reply is expected it names `CCB_REMOTE_REPLY_COMMAND`: answer by running exactly that command from the pane that received it, never with a reverse `ask --peer`.

## Notes

- Supported peer targets are Claude and Codex. Gemini and OpenCode remain same-project only.
- Multiple live sessions are an unresolved ambiguity, not an unconditional refusal: an exact current
  session selection may use `--live-id`.
- Claude and Codex peer messages are delivery-only; replies are explicit reverse peer messages.
- A local Codex answer is never captured or forwarded merely because it followed an inbound peer message.
- Do not narrate transport diagnostics unless delivery fails.
