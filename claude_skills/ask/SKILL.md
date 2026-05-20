---
name: ask
description: Async via ask, end turn immediately; use when user explicitly delegates to any AI provider (gemini/codex/opencode/droid); NOT for questions about the providers themselves.
metadata:
  short-description: Ask AI provider asynchronously
---

# Ask AI Provider (Async)

Send the user's request to specified AI provider asynchronously.

## Usage

The first argument must be the provider name, followed by the message:
- `gemini` - Send to Gemini
- `codex` - Send to Codex
- `opencode` - Send to OpenCode
- `droid` - Send to Droid

### Cross-Project Claude Bridge

Use `ccb-list` to discover active CCB projects and their target identifiers:

```
Bash(ccb-list)
```

Send a message to Claude in another active CCB project:

```
Bash(CCB_CALLER=claude ask --peer <path-or-index-or-hash-prefix> "$MESSAGE")
```

Targets accepted by `--peer`:
- Full project path from `ccb-list`
- List index, e.g. `1` or `[1]`
- CCB project hash prefix, minimum 4 hex characters

Examples:
- `ask --peer /home/musta/dev/content-automation "Review the current plan"`
- `ask --peer b0e3 "Status?"`
- `ask claude --peer 1 "Summarize your current task"`

## Execution (MANDATORY)

```
Bash(CCB_CALLER=claude ask $PROVIDER "$MESSAGE")
```

## Rules

- Follow the **Async Guardrail** rule in CLAUDE.md (mandatory).
- Local fallback: if output contains `CCB_ASYNC_SUBMITTED`, end your turn immediately.
- If submit fails (non-zero exit):
  - Reply with exactly one line: `[Provider] submit failed: <short error>`
  - End your turn immediately.

## Examples

- `/ask gemini What is 12+12?`
- `/ask codex Refactor this code`
- `/ask opencode Analyze this bug`
- `/ask droid Execute this task`
