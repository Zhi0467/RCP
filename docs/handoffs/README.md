# Active implementation handoffs

Active:

- [Finalize disconnected remote turns on their original tasks](handoff-2026-09-16-same-task-remote-finalization.md)
  — implemented for ordinary Work in PR #165; the full local suite and a
  fresh-process real-SSH loss journey pass. Transport loss leaves the original
  task waiting, reconnect automatically finalizes the same remote pass through
  Work's idempotent finalizer, unreachable hosts wait without timeout, Stop is
  never delayed, and invalid evidence requires human Retry. The served-browser
  journey and final-head PR CI remain. Draft PR #162 is evidence only and must
  be closed as superseded after this replacement is verified.

- [Episodes settle honestly, logins stay alive, reauthorization continues the work](handoff-2026-09-14-episode-lifecycle-and-provider-login.md)
  — design confirmed 2026-09-14 and revised after an xhigh review; all six
  slices implemented and driven against a copy of the production data;
  provider-dependent team-server and desktop checks remain: ending
  receipts compact and wrap-up failures settle visibly;
  an exhaustive health table; login failures stop retries and block launches;
  Codex per turn under a hardened gate, Claude on a static token, sign-in from
  the UI; reauthorization as a continuation episode on the same branch and
  session; a branch merges on branch facts; wake provenance and mail harvest;
  a typed episode timeline. Six slices on one pull request, none optional.

- [External job and watcher simplification](handoff-2026-09-06-external-job-simplification.md)
  — direct Slurm submission, one shell-watcher contract, human Cancel, and an
  OS-owned helper for ordinary processes. Integrated checks and the real
  team-server/Codex journey remain; the predecessor compute-runner plan is archived.

- [Claude queued follow-up](handoff-2026-09-08-claude-queued-follow-up.md) — queued provider turns, honest errors, and sandbox readiness implemented; integrated local/SSH verification remains open.

- [Worktree execution](handoff-2026-09-05-worktree-execution.md)
  — implemented and merged; local API/Git, Codex Work/merge, and browser-component checks verified; the full live journey remains open.

- [A refusal explains itself](handoff-2026-09-09-refusal-explains-itself.md)
  — human-confirmed on 2026-08-15 and not yet implemented: a refused Apply
  gets a terminal `refused` state and a plain-language explanation.

- [Remaining disposable supervisor qualification](handoff-2026-09-06-disposable-supervisor-qualification.md)
  — production adoption, promoted release, complete backup, and doctor are
  verified. The separate controller fix and unfinished Ubuntu reboot/restore
  cases remain; the original deployment handoff is archived.

The [phone-access handoff is closed](../archive/handoffs/handoff-2026-09-09-phone-access-device-sessions.md):
device sessions, the Devices panel, pairing, and single-session desktops shipped;
the tailnet is an operator procedure in `docs/server.md`.

The [team-space/server handoff is closed](../archive/handoffs/handoff-2026-08-27-dev-team-space-and-server.md):
the backed-up research project was transferred and verified through the desktop on
2026-09-05. Existing two-member production use was accepted by the human;
separate disposable-host SSH qualification was deliberately skipped. Unrun
coverage remains explicit, rather than being relabeled as passed.

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
