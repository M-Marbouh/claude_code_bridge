---
name: pend
description: View an exact CCB task reply or the current caller's latest provider task.
metadata:
  short-description: View latest AI provider reply
---

# Pend - View Task Reply

Pass an exact task ID for deterministic retrieval, or a provider name for the current caller's latest task.

## Usage

The first argument must be the provider name:
- `gemini` - View Gemini reply
- `codex` - View Codex reply
- `opencode` - View OpenCode reply
- `claude` - View Claude reply

## Execution (MANDATORY)

```bash
pend $ARGUMENTS
```

## Examples

- `/pend gemini`
- `/pend 20260711-120000-001-99`
- `/pend claude`
