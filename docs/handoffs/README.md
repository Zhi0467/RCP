# Active implementation handoffs

Active:

- [Dev team space and source server completion](handoff-2026-08-27-dev-team-space-and-server.md)
  — human-confirmed implementation through one genuinely usable lab/server
  deployment. Implementation is active directly on `main`. The gate, server
  foundation, provisioning, backup, restore, and member-removal lanes are
  implemented; two of them still owe a live drive. The desktop team-space lane
  is in progress, and the read-only Server Settings projection is complete.
  Transfer is implemented through validated history-only target import and
  imported-provider-history lifecycle support; archive relay/decode, activation,
  cleanup, UI, and the live lab drill remain.
  Read the handoff's packet status table for the current state of any packet.
  Its dated implementation log and completed packet sections were archived to
  [the evidence file](../archive/handoffs/handoff-2026-08-27-dev-team-space-and-server-evidence.md)
  on 2026-09-01; the handoff itself retains only work with an open drive.

This directory contains only human-confirmed work that is ready to implement and
not yet complete. A handoff is an execution contract, not a chronological diary.
Its opening status must state:

- what is already implemented and verified;
- what remains;
- which decisions are settled; and
- the exact condition for closure.

Update status and decisions in the same change that alters the implementation
plan. Work that was measured and rejected is closed, not “not done.” Never retain
contradictory old and new plans as simultaneous active instructions.

Archive a handoff under [`../archive/handoffs/`](../archive/handoffs/) as soon as
its work is completed, rejected, superseded, or abandoned. If a later effort
materially changes scope, archive the predecessor and create a new handoff rather
than appending a second plan. Archived files are historical evidence and never
current authority.

The closed backend structural-refactor rationale is recorded in
[the active decision record](../decisions/2026-08-20-backend-structural-refactor-closure.md).
Current behavior remains owned by [`../design.md`](../design.md) and the applicable
file under [`../specs/`](../specs/).
