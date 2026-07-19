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

**NOT for cross-project messaging.** If the user says "Ask PRG Claude..." or references another project, use the `peer-ask` skill instead.

## Execution (MANDATORY)

You MUST call the Bash tool with this exact command — do not skip it, do not simulate it:

```
Bash(CCB_CALLER=claude ask $PROVIDER "$MESSAGE")
```

## Rules

- **Never say "[Provider] processing..." unless the Bash output contains `[CCB_ASYNC_SUBMITTED`.** If you did not run the Bash call, or the output does not contain `CCB_ASYNC_SUBMITTED`, you have NOT submitted anything — do not pretend otherwise.
- Follow the **Async Guardrail** rule in CLAUDE.md (mandatory).
- Local fallback: if output contains `CCB_ASYNC_SUBMITTED`, end your turn immediately with `[Provider] processing...`
- If submit fails (non-zero exit) or `CCB_ASYNC_SUBMITTED` is absent from output:
  - Reply with exactly one line: `[Provider] submit failed: <short error or 'no async marker in output'>`
  - End your turn immediately.
- If `ask` fails with `CCB_ROUTE_ERROR ... reason=not_mounted`, check your cwd: run from the project root shown by `ccb-list`. Only shells without `CCB_RUN_DIR` (non-CCB-managed) are cwd-sensitive.

## Examples

- `/ask gemini What is 12+12?`
- `/ask codex Refactor this code`
- `/ask opencode Analyze this bug`
