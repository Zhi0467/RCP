# Active implementation handoffs

- [Standby voice robustness](handoff-2026-10-08-standby-voice-robustness.md)
  — design awaiting go: keep voice while the desktop window is hidden,
  saved transcripts with Resume, non-strict tool schemas, and broad read tools.
- [Agent link robustness](handoff-2026-09-29-agent-link-robustness.md)
  — implemented on its PR: chat wake sessions, event parsing, the validator
  poller, state-transfer retry, and Apply again; live checks and the remaining
  tool audit remain.

## Open live checks

These are manual drives the closed handoffs left unexecuted. Each needs real
hardware, a real provider login, or a disposable server, so no unit test
stands in for it. Run one on disposable data, then delete its line here.

- Machine dependencies: on a disposable remote Linux account missing a
  required program (for example `rsync` off the SSH PATH), confirm the machine
  card lists it with the apt command and a chat sent to that machine is refused
  before any task exists; restore it and **Check again** clears it. On a
  disposable team server, confirm `rcp server install` and `rcp server doctor`
  report a missing required program for the service account.
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
- Model-backed dictation: dictate through a connected OpenAI or Groq key from
  the desktop app in a personal space, and from the team browser app and phone
  web app, each member with their own key; dictate through a Gemini connection
  from at least one client; on macOS 26 or later, macOS dictation runs
  on-device through SpeechAnalyzer, including the first-use model download;
  `RCP Candidate.app` connects a service (proving the bundled check clips ship)
  and dictates on macOS 13, 14, or 15 through the old recognizer.
- Service model cards: with a real Groq key its dictation list loads and a
  connection saves (OpenAI and Gemini were checked live on 2026-10-04); in the
  desktop app, entering a team space right after a server update shows the new
  page without quitting.
- Standby voice agent: with a real OpenAI key in a rebuilt desktop app (the
  microphone usage string changed), open a session, ask about a project and
  hear a correct answer, and have it open a node; by voice, send a Work
  message, start an Experiment, and authorize Auto-research, once with **Tap to
  confirm** and once with **Run without confirming**, and hear each one finish,
  including after moving to another project; gracefully stop a running
  Experiment and Auto-research episode; run one terminal command; repeat from a
  team member's phone web app; a forgotten session ends at the idle limit, and
  closing or suspending the page ends the paid session.
- Agent browser: a Codex Work turn with Browser on, on a disposable team
  server, opens a localhost service the agent started there, and the session
  survives a service restart; a real OpenCode Work turn reuses a page and
  login across turns; killing the browser mid-turn leaves the turn complete
  with a lost notice.
- Agent secret hiding: after the team server updates, run doctor and one Work
  turn under the real unit (`PrivateTmp`, `NoNewPrivileges`) as the service
  account, confirming hidden secrets stay unreadable and Git, Slurm, and the
  browser keep working; push through a deploy key held by the backend's
  `ssh-agent`, and confirm the key stays readable when that agent is missing;
  check `bwrap` and the fallback warning on Ubuntu 24.04; repeat on the frozen
  candidate build.
- Personal sign-in: in the desktop app, native PDF preview, project transfer,
  and terminals through the owner session, and adopting a terminal-started
  backend by pasting its one-time code.
- Push notifications: in the installed Mac app, a native notification for a
  real Proposal opens that Proposal on click, both while running and from a
  click that launches the app; a Mac that was closed drops resolved and
  day-old items on its next launch and collapses more than three into one
  summary; a member's iPhone added to the Home Screen on a team space receives
  a push for a real Proposal and opens it.
- Live artifacts: on a served app with a real provider and disposable data, a
  chat turn's helper job writes a live loss curve that redraws while the job
  runs, stops when it ends, and still renders after the job's files are
  deleted; a viewer comment on a chat artifact, an Experiment turn's artifact,
  and an episode report each returns as the next version in its own session,
  Undo restores the previous one, and the next Experiment turn re-opens its
  master; the desktop viewer checks in `docs/desktop.md`.
- Agent questions: with a real provider and broker, a Work turn's `ask` is
  answered on the card within one client call and the turn continues; a parked
  question survives an RCP restart, and its later answer starts the next turn
  on the asking turn's session; an Auto-research orchestrator's question keeps
  the episode running, sends one "Needs you" push, and its answer wakes the
  orchestrator as human mail.
- Lid-closed keep-awake: the four "Lid-closed mode" checks in `docs/desktop.md`
  on a Finder-launched packaged candidate.
- Nightly consolidation: with a real provider on disposable data, an enabled
  schedule fires at its time, merges a seeded duplicate pair in one revision,
  and leaves one report that Keep moves into Artifacts; an unchanged main the
  next night starts no turn; a failed turn leaves a dismiss-only row naming
  committed revisions; an expired authorization starts nothing and shows in
  the settings card; a Work turn's `lesson add` shows in the Lessons card and
  the next launch's `lessons.md`, and a human-edited lesson refuses an agent
  update.
