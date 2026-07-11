<!-- MUTUAL_RATIFICATION_START -->
## Mutual Ratification

When Claude sends a substantive proposal through `ask codex`, verify the claim, evidence, and intended action, then return exactly one verdict:

- `concur`
- `concur-with-amendment`
- `contest`
- `insufficient-evidence`

Give the verdict in plain text followed by a short justification and any amendments. Implement only ratified work. After implementation, treat Claude's review findings as proposals and reply with `accept`, `valid issue, different fix`, `not a bug because...`, or `needs plan adjustment`.

There are no abstract roles, provider instances, workers, or sub-agents. Evidence drift reopens ratification.
<!-- MUTUAL_RATIFICATION_END -->
