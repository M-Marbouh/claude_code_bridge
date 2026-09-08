<!-- MUTUAL_RATIFICATION_START -->
## CCB Collaboration

Mounted sessions are co-equal collaborators: authority follows evidence, not identity. Provider identity, launch order, model, and account do not assign a role. Either session may propose, implement, review, challenge, or synthesize work when that is its current assignment.

The current explicit assignment for this session determines its role. It supersedes provider identity, launch order, recalled Memsearch history, and previous session roles. Historical role assignments are context only and must not establish current ownership. If no current assignment exists, do not infer one from history. Role assignment does not override standing engineering rules or grant additional action permissions.

Session role assignments are operational context, not architectural decisions. Automatic history capture may record them, but they must not be promoted into standing rules or ADRs. Durable decisions may define provider-neutral, session-assigned roles; they must not assign lasting ownership to whichever agent or model currently performs a role.

### Mutual Ratification

For substantive proposals sent through `ask`, include the claim, evidence, intended action, and material unknowns. The receiving session verifies the proposal and replies with `concur`, `concur-with-amendment`, `contest`, or `insufficient-evidence`, followed by a short justification. Implement only ratified work. Treat review findings as proposals that may be accepted, amended, contested, or returned for plan adjustment. Evidence drift requires fresh ratification.

Scale independent verification to risk. Preserve protocol, compatibility, and authorization boundaries regardless of current role. Git push, deployment, destructive actions, and other external effects still require their normal authorization; a role assignment alone never grants it.

### Top-level session boundary

CCB coordinates only mounted top-level sessions. It does not create or address native subagents, assign model or effort policy, or expose provider-instance routing names. Native in-session agent tools remain available under their product rules and are an execution strategy of the owning top-level session, not CCB routing topology.

The owning top-level session integrates and verifies internal-agent work. When another session reviews such work, label its provenance because the reviewer cannot infer it; review depth should scale with risk. Requests, receipts, and replies remain attributed to the verified owning top-level session.
<!-- MUTUAL_RATIFICATION_END -->
