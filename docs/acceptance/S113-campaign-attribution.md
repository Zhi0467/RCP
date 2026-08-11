---
id: S113-campaign-attribution
status: pending — not human-confirmed
tier: hermetic
driver: pytest + browser
covered_by: none
invariants: [1, 3, 4]
---

# Campaign work retains its authorization lineage

This scenario is deliberately deferred until the orchestrator lifecycle in S77
and S78 is human-confirmed and implemented. It is not part of base attribution
in S99.

The later design must decide how a project-orchestrator Patch identifies its
profile, campaign, root authorizer, direct task, parent task, and worker without
turning lifecycle ids into durable permission principals. It must also settle
which lineage belongs in permanent Patch history and which remains in receipts.

## Proposal boundary

- Campaign attribution extends the additive S99 envelope; it does not rewrite
  base or legacy Patches.
- A child uses ordinary semantic authority even when spawned by an orchestrator.
- No agent-produced Proposal is approved by an agent.
- The direct task that produced a Patch remains distinguishable from its root
  campaign and human authorizer.
- Replay never loads live campaign, task, membership, or permission records.

## Not yet decided

- the exact campaign, parent, and worker fields stored in the Patch envelope;
- the final immutable receipt schema;
- how stopped, resumed, and recovered campaigns present lineage in History; and
- the campaign-report link, retention, and failure behavior.
