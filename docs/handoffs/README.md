# Active implementation handoffs

Active:

- [Compute runner](handoff-2026-09-06-compute-runner.md) — human-confirmed
  2026-09-06; PR A foundation implemented on its branch, PRs B–E remain.
  Agents hand long-running computation
  to an OS-owned backend through the staged command client and are woken by a
  job observer; five stacked PRs in the documented order.

- [Live provider steering](handoff-2026-09-05-live-provider-steering.md) — Phase 2 implemented; local Codex API and browser-component receipts verified; full served-browser, Claude live and SSH verification open.

- [Worktree execution](handoff-2026-09-05-worktree-execution.md)
  — implemented and merged; local API/Git, Codex Work/merge, and browser-component checks verified; full acceptance remains open.

- [External supervisor and release artifacts](handoff-2026-09-02-external-supervisor-and-release-artifacts.md)
  — contract and build/release workflows implemented; public-source transition
  cleanup remains. Moves server
  update and restore out of the application into a Python supervisor that
  installs promoted release artifacts from `stable`, with one CI build per
  merge and human promotion. The private CLI connection remains for existing
  non-deployment operations, as confirmed on 2026-09-05. The supervisor package is not implemented.
  The first-lab closure gate is satisfied; finish Phase 2 cleanup, then dispatch
  Phases 3–6 in the documented short-PR order.

The [team-space/server handoff is closed](../archive/handoffs/handoff-2026-08-27-dev-team-space-and-server.md):
the backed-up CoT project was transferred and verified through the desktop on
2026-09-05. Existing two-member production use was accepted by the human;
separate disposable-host SSH qualification was deliberately skipped. Unrun
acceptance coverage remains explicit, rather than being relabeled as passed.

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
