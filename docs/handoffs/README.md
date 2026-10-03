# Active implementation handoffs

- [Standby voice agent](handoff-2026-10-02-voice-agent.md)
  — designed 2026-10-02 (issue #229, part 2); not implemented yet; builds on
  the model-backed dictation PR.
- [Agents ask the human through an `ask` verb](handoff-2026-10-01-ask-verb.md)
  — design settled 2026-10-01; implementation in progress on its PR.
- [Live artifacts and the docked viewer](handoff-2026-09-29-live-artifacts.md)
  — implemented 2026-09-30 on the same PR; served-app, desktop, and real-data checks remain.
- [Push notifications to the Mac and the phone](handoff-2026-09-28-push-notifications.md)
  — implemented 2026-09-28 on the same PR; real Mac and iPhone journeys remain.
- [Agent link robustness](handoff-2026-09-29-agent-link-robustness.md)
  — implemented on its PR: chat wake sessions, event parsing, the validator
  poller, state-transfer retry, and Apply again; live checks and the remaining
  tool audit remain.

## Open live checks

These are manual drives the closed handoffs left unexecuted. Each needs real
hardware, a real provider login, or a disposable server, so no unit test
stands in for it. Run one on disposable data, then delete its line here.

- Machine writable paths: on a disposable team server, pick a shared dataset
  folder with the folder picker on a machine card, then write into it from a new
  terminal, a Codex Work turn, and a systemd compute job; confirm a write into
  the RCP data folder under a grant of the service account's home fails, `/tmp`
  is writable from Codex Work, a remote chat started before the update resumes
  on its legacy `/tmp` stage, and command sockets stay reachable from both
  providers' sandboxes.
- Worktree execution: two chats editing one repository; Discuss in the bound
  chat seeing the worktree's edits; native session continuation, Pause,
  Resume, Retry, and app restart finding the same worktree; the same over SSH;
  each Integrate option's refusal and success; a rejected integration and
  Remove without losing unmerged commits.
- Supervisor recovery: the `supervisor-recovery-live` workflow on disposable
  Ubuntu 22.04 and 24.04 guests, covering reboot recovery, repeated rollback,
  protected restore, and offline update.
- Compute jobs: a long-running job through real Codex and a disposable Slurm
  queue, with provider exit, RCP restart, watcher wake, native-session
  continuation, child budget, child Stop, and Cancel; plus the Experiment-loop
  and child-Work handoffs and wakes, missing-handoff correction, Cancel of a
  stopped watcher, degradation when work is unobservable, the served-browser
  drive, and the slow staged-command deadline.
- Live steering: same-session Claude follow-ups and Codex injection through the
  served UI with real local and SSH providers, including transport loss and
  restart with an unacknowledged message, Work containment, and on the
  execution host both a working sandbox and the missing-sandbox readiness
  diagnostic.
- Episode lifecycle: device-code sign-in, the Claude token journey, an
  "Add N turns" continuation, a branch merge that runs to completion, and wake
  suppression (mail that arrived mid-turn or a child login failure spends no
  wake) on a team server with a real login.
- Project terminals: a conflicted `git rebase -i` in the Terminals destination,
  including after navigating away and back.
- Operator stops: the deploy-key stop panel driven from the desktop app against
  a real saved operator route. Known gap: **Copy server command** copies the
  bare `operator_argv` with no statement of where it runs; giving it an
  execution context is a separate contract change.
- Pushes: the live team-space push run and real remote-machine SSH.
- Phone UI: the real-iPhone journey and its screenshot review.
- Runs loading: the team-server measurement of first open against the 2 s target.
- Continuation prompts: with a real provider, an Experiment episode through two
  watcher wakes, its report, then Add N turns on the same session, checking the
  recorded prompts carry a pointer, no pointer, and a re-opened master in turn.
- Agents and branch episodes: with a real provider on a team server, start an
  Experiment on a graph branch from main and confirm its Agents row reads
  Working with a Branch tag, then Done, and opens its Runs card; start a second
  Run on a node that already has a human chat and confirm that chat is
  untouched; let a watcher wake a human chat and confirm its Activity row shows
  while it runs.
- Update notices: a `publish-desktop.yml` rerun after a failed upload; in the
  prebuilt app, the Download button, quit and reopen, and a team connection.
