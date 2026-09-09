# Active implementation handoffs

Active:

- [External job and watcher simplification](handoff-2026-09-06-external-job-simplification.md)
  — direct Slurm submission, one shell-watcher contract, human Cancel, and an
  OS-owned helper for ordinary processes. Integrated checks and the real
  team-server/Codex journey remain; the predecessor compute-runner plan is archived.

- [Claude queued follow-up](handoff-2026-09-08-claude-queued-follow-up.md) — queued provider turns, honest errors, and sandbox readiness implemented; integrated local/SSH acceptance verification remains open.

- [Worktree execution](handoff-2026-09-05-worktree-execution.md)
  — implemented and merged; local API/Git, Codex Work/merge, and browser-component checks verified; full acceptance remains open.

- [Phone access: device sessions and mobile rendering](handoff-2026-09-09-phone-access-device-sessions.md)
  — a member should see their own connected devices and revoke any one of them.
  Nothing is implemented yet; the authentication facts and the mobile scan are
  verified, and the transport question is deliberately excluded.

- [Remaining disposable supervisor qualification](handoff-2026-09-06-disposable-supervisor-qualification.md)
  — production adoption, promoted release, complete backup, and doctor are
  verified. The separate controller fix and unfinished Ubuntu reboot/restore
  cases remain; the original deployment handoff is archived.

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
