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

```bash
CCB_CALLER=codex ask $PROVIDER <<'EOF'
$MESSAGE
EOF
```

## Rules

- After running the command, say "[Provider] processing..." and immediately end your turn.
- Do not wait for results or check status in the same turn.
- The task ID and log file path will be displayed for tracking.
- If `ask` fails with `CCB_ROUTE_ERROR ... reason=not_mounted`, check your cwd: run from the project root shown by `ccb-list`. Only shells without `CCB_RUN_DIR` (non-CCB-managed) are cwd-sensitive.

## Examples

- `/ask gemini What is 12+12?` (send via heredoc)
- `CCB_CALLER=codex ask gemini <<'EOF'`
  `What is 12+12?`
  `EOF`

## Notes

- If it fails, check backend health with the corresponding ping command (`ccb-ping <provider>` (e.g., `ccb-ping gemini`)).
- Codex-managed sessions default to foreground; use `--background` or `CCB_ASK_BACKGROUND=1` for async.
