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

When a message contains `CCB_REPLY_MODE: automatic-capture`, complete the requested work and reply
normally in the current turn. CCB captures and routes the reply automatically. Do not invoke a reverse
`ask --peer` command.

Read the intent metadata:
- `notify` / `CCB_REPLY_EXPECTED: no`: consume the information; do not ask for follow-up.
- `wait` / `CCB_REPLY_EXPECTED: yes`: the sender is blocked; return the completed result promptly.
- `background` / `CCB_REPLY_EXPECTED: yes`: complete the work normally; the sender continued other work.

Return substantive results, not transport acknowledgements.

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
- Claude uses asynchronous delivery and a reverse peer result.
- Codex uses normal CCB completion capture and routes its response directly to the originating pane.
- Do not narrate transport diagnostics unless delivery fails.
