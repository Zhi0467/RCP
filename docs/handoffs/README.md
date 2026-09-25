# Active implementation handoffs

Active:

- [The phone works, and Chats shows every agent at a glance](handoff-2026-09-25-phone-ui-and-agents-panel.md)
  — design confirmed 2026-09-25 and revised after an xhigh review;
  implemented, with the real-iPhone journey and a screenshot review remaining. One pull request: a mechanical stylesheet split, size tokens
  with named phone and tablet widths, a phone pass on Inbox, Runs, Chat, and
  Settings, and an agent-hub Chats panel whose task list keeps every chat
  that still needs a human. Runs and the composer are unchanged.

- [Agents and terminals can push to their repositories](handoff-2026-09-24-agents-and-terminals-can-push.md)
  — confirmed and implemented 2026-09-24; the live team-space push run and
  real remote-machine SSH remain. The deploy key is the default push
  credential for the terminal, Discuss, and Work, with no per-member Git
  setting. RCP supplies a default commit identity that any Git config
  overrides.

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
  team-server/Codex journey remain.

- [Claude queued follow-up](handoff-2026-09-08-claude-queued-follow-up.md) — queued provider turns, honest errors, and sandbox readiness implemented; integrated local/SSH verification remains open.

- [Worktree execution](handoff-2026-09-05-worktree-execution.md)
  — implemented and merged; local API/Git, Codex Work/merge, and browser-component checks verified; the full live journey remains open.

- [A refusal explains itself](handoff-2026-09-09-refusal-explains-itself.md)
  — human-confirmed on 2026-08-15 and not yet implemented: a refused Apply
  gets a terminal `refused` state and a plain-language explanation.

- [Remaining disposable supervisor qualification](handoff-2026-09-06-disposable-supervisor-qualification.md)
  — production adoption, promoted release, complete backup, and doctor are
  verified. The separate controller fix and unfinished Ubuntu reboot/restore
  cases remain.

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

Delete a handoff in the same change that completes, rejects, supersedes, or
abandons its work. If a later effort materially changes scope, delete the
predecessor and create a new handoff rather than appending a second plan. Git
history is the record; rationale that must outlive the work belongs in a
decision record.

The closed backend structural-refactor rationale is recorded in
[the active decision record](../decisions/2026-08-20-backend-structural-refactor-closure.md).
Current behavior remains owned by [`../design.md`](../design.md) and the applicable
file under [`../specs/`](../specs/).
