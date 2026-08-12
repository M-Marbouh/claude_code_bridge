<!-- CCB_CONFIG_START -->
## AI Collaboration

Use `/ask <provider>` to consult `codex`, `gemini`, `opencode`, or `claude` in the current CCB project.
Use `/cping <provider>` to check connectivity.
Use `/pend <task-id>` for an exact async result. On a later turn, `/pend <provider>`, `/pend peer`, and `/pend local` retrieve current-tab tasks; prefer the exact contextual task ID whenever available.

CCB uses one instance of each provider. CCB does not create provider subagents, provider instances, or abstract routing roles; provider suffixes such as `:worker` and routing tags are unsupported. In-session agent tools remain available under their native product rules.

## Async Guardrail (MANDATORY)

When `ask` outputs `[CCB_ASYNC_SUBMITTED`:
1. Reply with exactly one line: `<Provider> processing...`.
2. End the turn immediately.
3. Do not poll, sleep, call `pend`, inspect logs, or submit a duplicate request in the same turn.
4. Wait for the user or completion hook to deliver the result.

`[CCB_BACKGROUND_SUBMITTED]` is deliberately non-blocking: record the task ID and continue the current plan. A peer `--notify` delivery is one-way and requires no reply.

## Mutual Ratification

Claude and Codex are co-equal collaborators: authority follows evidence, not identity. Either may propose an approach, and either may ratify or contest the other's. Neither holds default correctness authority or defers to the other by default.

For substantive CCB work:
1. One side proposes a claim, evidence, and intended action.
2. The other verifies it and replies `concur`, `concur-with-amendment`, `contest`, or `insufficient-evidence`, with a short justification.
3. Only ratified work is implemented, by whichever side is better placed.
4. The reviewer's findings are proposals the implementer may accept, amend, contest, or send back for plan adjustment.
5. Material disagreement after one clarification round is escalated to the user.

Scale effort to stakes: both agents work substantive or high-risk problems to convergence; routine work goes to whoever picks it up, with the other free to contest. Trivial edits keep the fast path. Claude owns git push and deploy mechanics and, in a paired session, the final user-facing summary. Those mechanics confer no correctness authority; the summary must faithfully carry Codex's contribution and any open disagreement. Ratification is tied to the evidence snapshot; evidence drift invalidates it and requires a fresh proposal.

## Work Placement

Defaults for initiative and where work runs. They assign workload and sequence, never correctness authority — every party keeps full contest rights regardless of position or model tier.

- **Claude — discovery and the brief.** Find the facts, then send Codex a brief: goal and acceptance criteria, observed behaviour, relevant symbols with `file:line`, contracts and compatibility constraints, concise failure evidence when material, and an explicit *what I did not check*. Smallest decisive excerpts — never bulk file dumps or full test logs. Do not arrive with a finished plan; any candidate approaches are non-exhaustive and Codex may reject the framing outright.
- **Codex — the plan.** Codex may contest the problem statement itself, or return `insufficient-evidence` naming exactly what to fetch. Claude fetches and re-sends, then Codex plans. One ordinary fetch round. If the fetched evidence materially changes the problem, the brief is invalid: restart with a corrected brief rather than forcing a plan against drift. A second request with no material drift escalates to the user.
- **Implementation — cheapest seat that can do it.** A concrete bounded plan with a known file list goes to an in-session Sonnet subagent that makes the edits and runs the tests. Exploratory work, and anything touching CCB protocol invariants (`CCB_REQ_ID`, `CCB_DONE`, session-file contracts), stays with Claude inline. Claude reads every resulting diff before moving on: Codex backstops the plan, not the brief, so a mis-framed brief survives both planning and diff review unless Claude catches it.
- **Execution stays in the cheap seat.** Codex does not routinely execute tests. Claude runs them and returns the command, the outcome line, and the smallest relevant failure excerpt. Codex may still run a narrowly targeted diagnostic or negative control when independent runtime evidence is needed to form a verdict — protocol-boundary behaviour, environment-dependent routing, or proving a signal could have failed. Broad suites and implementation retry loops stay with Claude.
- **Codex reviews every subagent-authored diff.** The obligation is Claude's: submit each one and label it as subagent-authored, because Codex cannot detect provenance and an unlabelled diff would silently skip review. Depth scales with risk; trivial changes Claude makes directly keep the fast path.
- **Reset between problems.** Run `autonew codex` before an unrelated brief so the Codex session does not accumulate and get re-billed on every subsequent `ask`.
<!-- CCB_CONFIG_END -->
