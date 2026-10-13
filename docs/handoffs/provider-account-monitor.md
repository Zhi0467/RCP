# Provider account monitor

Status: design, not started. Nothing below is implemented. Implementation waits
for an explicit start.

## Problem

A provider limit (`session_limit`, the umbrella name for every kind of limit)
fails the turn. Recovery now resumes the same session, but nothing notices when
the limit lifts. The automatic reattempt ladder retries on a fixed schedule and
stops after a repeat, so work stays failed until a human presses Retry. On
2026-10-12 an Auto-research orchestrator hit a Claude usage limit, was retried
twice before the limit reset, and stopped.

## What already exists

- `ProviderAccounts` (`src/rcp/agents/provider_accounts.py`) owns one durable
  login state per `(provider, host)` account: `signed_in` or `signed_out`, with
  a generation.
- `BackgroundAgentTasks.admit_provider_task` is the shared admission fence.
  While an account is `signed_out` it refuses new paid work. Queued tasks stay
  queued, Auto-research recoveries are created `blocked`, and mail, notices, and
  watcher completions stay pending.
- `ProviderSignInRunner.verify` runs the profile's `login_probe_command`, one
  minimal real request on the execution host. Success marks the account
  `signed_in` and calls `resume_provider_account`
  (`src/rcp/runs/provider_login.py`), which releases parked work once.
- The browser grant is the model for a per-launch toggle: `browser_requested`
  on the episode row, `chat_browser_preferences` per chat, and one shared
  `web/src/core/BrowserControls.tsx` at every entry point.

## Design

1. **One state owner.** `ProviderAccounts` gains a `limited` state beside
   `signed_in` and `signed_out`. Login validity and capacity stay distinct.
2. **Passive observation.** A turn that fails with a limit marks its account
   `limited`. No new prose parsing beyond the profile's classification.
3. **Park through the existing fence.** While `limited`, `admit_provider_task`
   parks paid work as for `signed_out`.
4. **Probe only while work waits.** One loop in the backend process probes a
   `limited` account with `login_probe_command`, only while opted-in work is
   parked. Interval 5, 10, 20, then 30 minutes, with jitter and no overlapping
   probes, in `limits.py`. A probe is a real model request.
5. **Release through the owners.** A successful probe marks the account
   `signed_in` and releases parked work through each work kind's own recovery
   owner. Parking spends no recovery attempt. A probe never resets an exhausted
   or repeat-blocked ladder.
6. **Auto-recover is a launch option, and launch options become one object.**
   Today each per-launch runtime choice has its own path: the browser grant
   (`browser_requested` column, `chat_browser_preferences`, `BrowserToggle`)
   and the code worktree (a `codeWorktree` checkbox feeding `EpisodeIsolation`).
   Auto-recover would be a third. Instead, introduce one `LaunchOptions` model
   (browser, worktree, auto-recover) with one storage owner on the episode and
   the chat, one field on every launch request, and one web control used at
   Experiment and Auto-research start, Agents, and the chat composer. Browser
   and worktree move onto it first, with no behavior change; auto-recover is
   then a new field, not a new path. The web control is the chat composer's
   existing Options fold (`chat-options-picker` in `NodeChat.tsx`: a sliders
   button showing how many options are on, opening the Browser and Worktree
   rows). It becomes one shared component, replacing the flat toggles in
   `AutoResearchDialog`, `ExperimentRunDetail`, and `DetailDrawer`, and gains an
   Auto-recover row. Each option's enforcement stays with its
   owner (browser runtime, isolation, recovery); only the choice is shared.
   Children, reports, and wakes inherit their episode's options. Auto-recover
   off means the work stays parked across account recovery and backend restart
   until a human acts.
7. **Projection.** The provider card shows the account state and a manual
   Verify. It never holds the recovery switch: account health is shared,
   permission to spend is not. Usage numbers are out of scope.

The monitor never calls RCP's HTTP API. It runs in the backend process, reaches
the provider only through its CLI on the execution host, and releases work
in process.

## Gaps the implementation must close (from review)

- Release covers queued tasks and `provider_auth` recoveries only. It misses
  failed ordinary chat and Agents turns, Experiment turns failed by a limit,
  repeat-blocked Auto-research recoveries, failed workers and child Work (use
  their owner's route), and reports (their loop spends all three attempts on a
  limit).
- `AdmissionRefused` is a `ValueError`. Child admission reconciliation and child
  Experiment kickoff treat that as permanent and cancel. Parking must not cancel.
- Recovered remote failures settle through `fail_agent_task` without a failure
  kind, so a limit there is never observed.
- "Off" is not enforced today: `verify()` reconciles, later lifecycle passes
  release every unrecovered `signed_in` generation, and polling retries pending
  recoveries once admission succeeds. The work-level flag must gate all three.
- `verify` overwrites the generation unconditionally. A probe must use a
  captured availability revision and a conditional update, so a stale probe
  never clears a newer limit or a sign-out.
- `docs/specs/providers-and-containment.md` says limit scope is not modeled and
  repeated failure stops automatic attempts. Update it in the same change: a
  limit is account-level.

## Settled decisions

- **Worktree is a launch option.** All three (browser, worktree, auto-recover)
  live on `LaunchOptions`. The worktree's allocation and Merge cleanup stay with
  `EpisodeIsolation`; only the choice moves.
- **A limit is account-level.** `session_limit` means the provider's 5-hour or
  weekly usage limit, so any limit parks the whole account. No per-signature
  scope. Rename `session_limit` to `limit` as part of the work.
- **Accounts are per `(provider, host)`.** One subscription on two hosts gets
  two rows and two probes; accepted for now.
- **Success does not clear `limited`.** A turn that succeeds may predate the
  limit or use another model; only a probe clears it.

## Checks when implemented

- Unit: a limit marks the account `limited` and parks new paid admission; a
  successful probe releases each kind of parked work once; a stale probe does
  not clear a newer limit.
- Integration: a stubbed provider fails with a limit, then succeeds. The episode
  continues on its saved session without a human Retry when auto-recover is on,
  and stays parked across a backend restart when it is off.
- Live: a real Claude usage limit on a disposable team server.
