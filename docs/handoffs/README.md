# Active implementation handoffs

Active:

- [A human stop says what to do, and where to run it](handoff-2026-09-19-operator-action-says-where-to-run-it.md)
  — design confirmed 2026-09-19 against a rendered mockup and implemented the
  same day. A command action names the shell it runs in, a pause is titled for
  the human's task, and the stop renders as an ordered list of single actions
  with copy controls in the desktop and transfer panels and a matching label in
  the CLI wizard. Python, web, browser, and native checks pass against a stop
  from the real builder, and a step now declares each action's name, its one
  requirement, and whether a value is pasted or only compared. Driving the
  panel from the desktop app, so the sign-in line comes from a real saved
  operator route rather than its fixture, remains.
- [A terminal in a project, fenced to its repositories](handoff-2026-09-19-a-terminal-fenced-to-its-project.md)
  — design confirmed 2026-09-19; the manager, routes, WebSocket, Terminals
  destination, per-machine capability, PTY-over-SSH, and their tests are
  implemented on one pull request. Its trust boundary is settled in
  [a decision record](../decisions/2026-09-19-a-member-terminal-inherits-the-work-trust-boundary.md):
  the terminal inherits the Work turn's boundary rather than isolating from the
  service account, so it must never describe itself as containment, and
  canonical state stays refused wherever the OS can refuse it. The Linux merge
  qualification passed on 2026-09-20: real mounts, interactive Git with the
  account's key, PTY resize, orphan cleanup, and a restart with an
  unreachable-host record. Remaining: the closure journey driven from the
  Terminals destination, and one team-space run where Git uses the deploy key.

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

The [same-task remote finalization handoff is complete](../archive/handoffs/handoff-2026-09-16-same-task-remote-finalization.md):
ordinary Work and its automatic corrections retain their task across connection
loss. Real-SSH restart and served-browser recovery journeys are verified. PR #165
awaits human merge; draft PR #162 remains superseded evidence.

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
