---
name: ask
description: Async via ask, end turn immediately; use when user explicitly delegates to gemini, codex, opencode, or claude within the CURRENT project. NOT for cross-project Claude messaging.
metadata:
  short-description: Ask AI provider asynchronously
---

# Ask AI Provider (Async)

Send the user's request to a provider running in the **current** CCB project.

## Usage

The first argument must be the provider name, followed by the message:
- `gemini` - Send to Gemini
- `codex` - Send to Codex
- `opencode` - Send to OpenCode
- `claude` - Send to Claude

**NOT for cross-project messaging.** If the user says "Ask PRG Claude..." or references another project, use the `peer-ask` skill instead.

## Execution (MANDATORY)

You MUST call the Bash tool with this command — do not skip it, do not simulate it. Add `--background` after the provider only when there is independent work to do:

```
Bash(CCB_CALLER=claude ask $PROVIDER "$MESSAGE")
```

## Rules

- Treat `[CCB_ASYNC_SUBMITTED` and `[CCB_BACKGROUND_SUBMITTED` as successful submission markers. If neither appears, the request was not submitted.
- For `--background`, retain the exact task ID. Continue only with independent work; never claim, act on, or build on the result until a complete reply matching that task ID arrives. If the result is needed and has not arrived, run `pend <exact-task-id>`; do not use implicit or provider-based `pend` for this task.
- Use `--background` only when there is independent work to do. For an async submission without `--background`, follow the **Async Guardrail** rule in CLAUDE.md and end your turn immediately with `[Provider] processing...`.
- If submit fails (non-zero exit) or both submission markers are absent from output:
  - Reply with exactly one line: `[Provider] submit failed: <short error or 'no async marker in output'>`
  - End your turn immediately.
- If `ask` fails with `CCB_ROUTE_ERROR ... reason=not_mounted`, check your cwd: run from the project root shown by `ccb-list`. Only shells without `CCB_RUN_DIR` (non-CCB-managed) are cwd-sensitive.
- In a verified `ccb codex codex` pair, `ask codex` from either member addresses the other member. Unknown or ambiguous callers fail instead of guessing.

## Examples

- `/ask gemini What is 12+12?`
- `/ask codex Refactor this code`
- `/ask opencode Analyze this bug`
