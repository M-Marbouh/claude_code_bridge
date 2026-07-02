<!-- CCB_CONFIG_START -->
## AI Collaboration
Use `/ask <provider>` to consult other AI assistants (codex/gemini/opencode/droid).
Use `/cping <provider>` to check connectivity.
Use `/pend <provider>` to view latest replies.

Providers: `codex`, `gemini`, `opencode`, `droid`, `claude`

## Async Guardrail (MANDATORY)

When you run `ask` (via `/ask` skill OR direct `Bash(ask ...)`) and the output contains `[CCB_ASYNC_SUBMITTED`:
1. Reply with exactly one line: `<Provider> processing...` (use actual provider name, e.g. `Codex processing...`)
2. **END YOUR TURN IMMEDIATELY** — do not call any more tools
3. Do NOT poll, sleep, call `pend`, check logs, or add follow-up text
4. Wait for the user or completion hook to deliver results in a later turn

This rule applies unconditionally. Violating it causes duplicate requests and wasted resources.

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
When a skill references a role (e.g. `reviewer`), resolve it to the provider listed here (e.g. `/ask codex`).
<!-- CCB_ROLES_END -->

<!-- MUTUAL_RATIFICATION_START -->
## Mutual Ratification

Principle: the `designer`'s inspection/review conclusions are PROPOSALS, not directives. Applies to substantive/high-impact work; trivial edits keep a fast path.

1. **Propose** — `designer`: claim + evidence + intended action (a concrete diagnosis, not broad exploration notes).
2. **Ratify** — `reviewer` verdict: concur / concur-with-amendment / contest / insufficient-evidence.
3. **Act** — `reviewer` (engaged via `/ask codex`, see the `delegate` skill) implements only ratified work.
4. **Validate** — `designer` reviews the result. HIGH-RISK diffs get an independent Opus subagent (fresh eyes) given the diff + agreed plan + success criteria. High-risk = schema/contract, auth/security, prompt behavior, cross-service, migrations, prod workflows, broad frontend state. Ordinary low-risk multi-file changes get `designer` review alone.
5. **Ratify Fixes** — `designer` findings are PROPOSALS; `reviewer` verdict: accept / "valid issue, different fix" / "not a bug because…" / "needs plan adjustment". Never conclude-and-direct.
6. **Escalate** — material disagreement surviving one clarification round → `/debate`, with both evidence sets, forced to an outcome (choose `designer`'s plan / choose `reviewer`'s plan / run a small verification experiment / defer).

Guardrails:
- **Ceremony-light default**: claim/evidence/action ↔ concur/contest/amend; proceed unless disagreement is MATERIAL. No heavier templates unless high-impact.
- **Stale evidence**: ratification binds to the evidence snapshot; material code drift REOPENS step 2 or 5.
- **Ownership**: `designer` still owns synthesis and final integration, SUBJECT TO ratification at steps 2 and 5.
- **Layering** (not merged): `delegate` (briefs) → Mutual Ratification (light, always-on for substantive work) → `/debate` (escalation only) → validate/review mechanics (Opus subagent for high-risk).
<!-- MUTUAL_RATIFICATION_END -->

<!-- GEMINI_INSPIRATION_START -->
## Inspiration Consultation

For creative tasks (UI/UX design, copywriting, naming, brainstorming), the `designer` SHOULD consult `inspiration` (via `/ask`) for reference ideas.
The `inspiration` provider is often unreliable — never blindly follow. Exercise independent judgment and present suggestions to the user for decision.
<!-- GEMINI_INSPIRATION_END -->

<!-- CCB_CONFIG_END -->
