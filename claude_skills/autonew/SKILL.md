---
name: autonew
description: Send /new to a provider's pane to start a new session without context injection.
metadata:
  short-description: Start new session in provider
---

# Auto New Session

Send `/new` command directly to a provider's terminal pane to start a new session.

## Usage

```
autonew <provider>
```

Providers: gemini, codex, opencode, claude, pi

For `pi` the command refuses (exit 1, nothing typed) unless the project publishes exactly one live Pi
session and its pane is confirmed idle.

## Execution (MANDATORY)

```bash
autonew $PROVIDER
```

## Rules

- This command sends `/new` directly to the provider's pane without any wrapping.
- Use this to clear/reset a provider's session.

## Examples

- `/autonew gemini` - Start new Gemini session
- `/autonew codex` - Start new Codex session
- `/autonew pi` - Start new Pi session (gated on a unique, idle Pi pane)
