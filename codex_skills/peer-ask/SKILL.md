---
name: peer-ask
description: Send messages to Claude or Codex panes in another active CCB project when the user names another project, asks another project's provider, or requests cross-project messaging. Use ask for same-project providers; Gemini and OpenCode are not supported cross-project.
---

# Peer Ask — Cross-Project Provider Messaging

Send a request to a mounted Claude or Codex pane in another active CCB project.

## When to Use

Use this skill for requests such as:
- "Ask PRG Claude about X"
- "Ask PRG Codex to review X"
- "Tell content-automation Claude..."
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
confirmation. If `CCB_PEER_TASK_ID` is missing, omit `--reply-to` rather than inventing an ID. Do not
produce a local `CCB_DONE`; peer delivery is complete once the message reaches this pane.

## Send a Peer Request

### 1. Discover targets

Run:

```bash
ccb-list --json
```

Each project exposes `work_dir`, `ccb_project_id`, `peer_providers`, and detailed provider status.
Only select a provider listed in `peer_providers`.

If the user names Claude or Codex, require that provider. If no provider is named, prefer Claude for
backward compatibility; when Claude is unavailable and Codex is the sole peer provider, select Codex.

Match projects by remembered alias, exact directory basename, clear abbreviation, or list index. Ask
the user only when multiple projects remain plausible.

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

Use the exact `work_dir` returned by `ccb-list`.

- `CCB_ASYNC_SUBMITTED` for `--wait`: end the turn immediately.
- `CCB_BACKGROUND_SUBMITTED`: continue current work; do not poll immediately.
- `CCB_NOTIFY_SUBMITTED`: continue after submission.
- A `--notify` message must not end with a direct question.

## Notes

- Supported peer targets are Claude and Codex. Gemini and OpenCode remain same-project only.
- Claude and Codex peer messages are delivery-only; replies are explicit reverse peer messages.
- A local Codex answer is never captured or forwarded merely because it followed an inbound peer message.
- Do not narrate transport diagnostics unless delivery fails.
