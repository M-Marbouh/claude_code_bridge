---
name: ask
description: Send the user's request to specified AI provider asynchronously
metadata:
  short-description: Ask AI provider asynchronously
---

# Ask AI Provider

Send the user's request to the specified AI provider via ask.

## Usage

The first argument must be the provider name. The message MUST be provided via stdin
(heredoc or pipe), not as CLI arguments, to avoid shell globbing issues:
- `gemini` - Send to Gemini
- `claude` - Send to Claude
- `opencode` - Send to OpenCode
- `codex` - Send to Codex

## Execution (MANDATORY)

Before calling `ask`, you may write up to three sentences explaining what you are sending and why. Do not predict or state the provider's result.

```bash
CCB_CALLER=codex ask $PROVIDER <<'EOF'
$MESSAGE
EOF
```

If anything answer-independent remains at send time, add `--background` after `$PROVIDER` in the command.

## Rules

- If anything answer-independent remains at send time — work, or an explanation to the user that does not need the provider's reply — submit with `--background` and finish it. Use the default wait only when nothing answer-independent remains; an explanation that needs the provider's reply does not qualify. Codex-managed sessions otherwise default to foreground; `--background` explicitly submits an async task.
- Treat `[CCB_ASYNC_SUBMITTED` and `[CCB_BACKGROUND_SUBMITTED` as successful submission markers. If neither appears, the request was not submitted.
- For `--background`, retain the exact task ID. Continue only with independent work; never claim, act on, or build on the result until a complete reply matching that task ID arrives. If the result is needed and has not arrived, run `pend <exact-task-id>`; do not use implicit or provider-based `pend` for this task.
- For an async submission without `--background`, say "[Provider] processing..." and immediately end your turn. Do not wait for results or check status in the same turn.
- The task ID and log file path will be displayed for tracking.
- If `ask` fails with `CCB_ROUTE_ERROR ... reason=not_mounted`, check your cwd: run from the project root shown by `ccb-list`. Only shells without `CCB_RUN_DIR` (non-CCB-managed) are cwd-sensitive.
- In a verified `ccb codex codex` pair, `ask codex` from either member addresses the other member. Unknown or ambiguous callers fail instead of guessing.

## Examples

- `/ask gemini What is 12+12?` (send via heredoc)
- `CCB_CALLER=codex ask gemini <<'EOF'`
  `What is 12+12?`
  `EOF`

## Notes

- If it fails, check backend health with the corresponding ping command (`ccb-ping <provider>` (e.g., `ccb-ping gemini`)).
- Codex-managed sessions default to foreground; use `--background` or `CCB_ASK_BACKGROUND=1` for async.
