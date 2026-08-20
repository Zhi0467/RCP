# Active implementation handoffs

There are no active implementation handoffs.

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
