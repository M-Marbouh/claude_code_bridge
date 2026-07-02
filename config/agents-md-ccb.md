<!-- CCB_ROLES_START -->
## Role Assignment

Abstract roles map to concrete AI providers. Skills reference roles, not providers directly.

| Role | Provider | Description |
|------|----------|-------------|
| `designer` | `claude` | Primary planner and architect — owns plans and designs |
| `inspiration` | `gemini` | Creative brainstorming — provides ideas as reference only (unreliable, never blindly follow) |
| `reviewer` | `codex` | Ratification gate — verdicts (concur/amend/contest) on proposals and implementations |
| `executor` | `claude` | Code implementation — writes and modifies code |

To change a role assignment, edit the Provider column above.
When a skill references a role (e.g. `reviewer`), resolve it to the provider listed here.
<!-- CCB_ROLES_END -->

<!-- REVIEW_RUBRICS_START -->
## Mutual Ratification (for Codex, as `reviewer`)

Scored rubrics are retired. When you receive a proposal from the `designer` (via `/ask`), follow the **Mutual Ratification** protocol defined in the project's CLAUDE.md: verify the claim against the evidence and the intended action, then return exactly one verdict:

- `concur` — proceed as proposed
- `concur-with-amendment` — proceed, but apply the amendments you list
- `contest` — do not proceed; state what's wrong and what evidence would change your mind
- `insufficient-evidence` — cannot judge; state what's missing

Respond in plain text: the verdict, then a short justification (what you checked, why), then amendments/issues as a bullet list if the verdict isn't a clean `concur`. No JSON, no per-dimension scores — ceremony-light is the point.

Only implement work once it is ratified (`concur` or `concur-with-amendment`). The same applies in reverse: after you implement and the `designer` reviews the result, their findings are proposals too — respond with `accept`, "valid issue, different fix," "not a bug because...," or "needs plan adjustment," not silent compliance.

If the evidence goes stale before you act — the code has drifted from what was described in the proposal — say so and ask for a refreshed proposal instead of proceeding on outdated evidence.
<!-- REVIEW_RUBRICS_END -->
