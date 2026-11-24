---
name: peer-ask
description: Send messages to a Pi or Claude or Codex pane in another active CCB project when the user names another project, asks another project's provider, or requests cross-project messaging. Use ask for same-project providers; Gemini, OpenCode and Hermes are not supported cross-project.
---

# Peer Ask — Cross-Project Provider Messaging (Pi)

Send a request to a mounted Pi, Claude or Codex pane in another active CCB project, and answer the
requests other projects send to you.

**Pi is reached only when it is named.** `ask pi --peer <target>` addresses a Pi pane; a bare
`ask --peer` still selects Claude, and Claude stays the default wherever it is available. Never
assume a project's Pi is the intended destination just because the project has one.

## When to Use

- "Ask BSA Pi about X"
- "Ask BSA Claude about X", "Tell web-frontend Codex..."
- "Send this to Pi in my other project"

Do not use this skill for a provider in the current project; use `ask` instead.

## Inbound Peer Requests

Read the intent metadata carried by the delivered message:
- `notify` / `CCB_REPLY_EXPECTED: no`: consume the information; do not send a reverse peer message.
  Any embedded question is informational.
- `wait` / `CCB_REPLY_EXPECTED: yes`: the sender is blocked; complete the work and answer promptly.
- `background` / `CCB_REPLY_EXPECTED: yes`: complete the work and answer when ready; the sender
  continued other work.

For a reply-bearing request, use `CCB_REPLY_TARGET` as the peer target and `CCB_REPLY_PROVIDER` as the
provider (fall back to `claude` when it is absent). Complete the work first, then send one substantive
reply from your bash tool:

```bash
ask <CCB_REPLY_PROVIDER> --peer "<CCB_REPLY_TARGET>" --reply-to "<CCB_PEER_TASK_ID>" --background <<'EOF'
<terminal result>
EOF
```

- Use `--background`, not `--wait`: `--wait` blocks your own tool call for the whole round trip.
- Keep the heredoc body **quoted** (`<<'EOF'`) so the shell does not interpret the reply.
- Use `--notify` instead of `--background` only for a terminal answer that asks no follow-up question.
- Omit `--reply-to` when `CCB_PEER_TASK_ID` is missing; never invent one.
- Never add `--live-id` to a correlated reply: the receipt pins the return endpoint.
- Do not produce a local `CCB_DONE`: CCB is not capturing this turn.

Do not acknowledge a peer message ("Got it", "Thanks"). Send the completed result, once.

## Send a Peer Request

### 1. Discover targets

```bash
ccb-list --json
```

Parse the fresh JSON array. Each project exposes `work_dir`, `ccb_project_id`, `providers` and a
`sessions` array. Do not discard a project because a provider is missing from the aggregate
`peer_providers`; inspect `sessions[].providers[provider]` when the aggregate is ambiguous.

If the user names a provider, require it. If none is named, prefer Claude, then a project whose only
mounted peer provider is another one.

Match projects by remembered alias, exact directory basename, a clear abbreviation, or the list index.
Ask the user only when several projects remain plausible.

A usable candidate has a non-empty `live_id`, `alive: true` and `mounted: true`. When exactly one is
usable, send without a selector. When several live sessions of that provider exist, never guess: use a
`live_id` the user supplied, or match a supplied `pane_id` or `live_slot` inside this project and
provider only, require exactly one current match, and use that candidate's `live_id`. If you cannot map
it uniquely and safely, show the candidates and ask.

### 2. Choose intent

- `--wait`: the current task cannot proceed without the answer.
- `--background`: the answer is useful later and current work can continue.
- `--notify`: one-way status or handoff with no answer expected (must not end with a question).

### 3. Send

```bash
ask <provider> --peer "<work_dir>" --background <<'EOF'
<message>
EOF
```

Use the exact `work_dir` from the fresh `ccb-list` output. For an explicitly selected session add the
identity as a discrete option, never inside `work_dir` or the message:

```bash
ask <provider> --peer "<work_dir>" --live-id "<live_id>" --background <<'EOF'
<message>
EOF
```

- `CCB_ASYNC_SUBMITTED` for `--wait`: end the turn immediately.
- `CCB_BACKGROUND_SUBMITTED`: continue current work; do not poll immediately.
- `CCB_NOTIFY_SUBMITTED`: continue after submission.

## Another Machine

A machine named with a project ("ask server demo Claude to review X") is another machine, addressed as
`--peer <host>::<project>`. Cross-machine delivery supports Claude and Codex only: Pi is not a
cross-machine target yet, and a Pi session has no cross-machine identity. For everything else follow
`ccb-list`'s "Other machines" section, match the host and project aliases exactly, and refuse to guess.

## Notes

- Supported local peer targets: Pi, Claude, Codex. Gemini, OpenCode and Hermes remain same-project only.
- Peer messages are delivery-only: the receiver answers with an explicit reverse `ask --peer`.
- A Pi destination is pasted into only when Pi reports itself idle with an empty editor. A busy pane is
  waited for; a draft or an open dialog is refused. If a delivery is refused or times out, the message
  status file (`CCB_ASYNC_STATUS_FILE`) and `pend <task-id>` show what happened.
- Do not narrate transport internals (`anchor_seen`, confirmation levels) unless delivery fails.
