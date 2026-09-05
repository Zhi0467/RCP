# Active implementation handoffs

Discussion draft in this unmerged planning PR:
[Live provider steering](handoff-2026-09-05-live-provider-steering.md). This is not
ready implementation work; settle its decisions before merging into the active plan.

Active:

- [Dev team space and source server completion](handoff-2026-08-27-dev-team-space-and-server.md)
  — human-confirmed implementation through one genuinely usable lab/server
  deployment. The direct-`main` exception ended on 2026-09-02; remaining work
  on this handoff uses short-lived branches, PR CI, and human merge. The gate,
  server foundation, provisioning, backup, restore, and member-removal lanes are
  implemented; two of them still owe a live drive. The desktop team-space lane
  is in progress, and the read-only Server Settings projection is complete.
  Transfer implementation, including relay, activation, cleanup, and UI, is
  complete; its source-built desktop/SSH lab drive remains.
  Read the handoff's packet status table for the current state of any packet.
  Its dated implementation log and completed packet sections were archived to
  [the evidence file](../archive/handoffs/handoff-2026-08-27-dev-team-space-and-server-evidence.md)
  on 2026-09-01; the handoff itself retains only work with an open drive.
- [External supervisor and release artifacts](handoff-2026-09-02-external-supervisor-and-release-artifacts.md)
  — human-confirmed on 2026-09-02; nothing implemented yet. Moves server
  update and restore out of the application into a Python supervisor that
  installs promoted release artifacts from `stable`, with one CI build per
  merge and human promotion. Phases 0–2 (contract, builds, going public) may
  start now; Phases 3–6 wait for the team-server handoff above to archive.

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
