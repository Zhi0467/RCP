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
- While an account is `signed_out`, the shared admission fence parks paid work
  instead of failing it: queued tasks stay queued, Auto-research recoveries are
  created `blocked`, and mail, notices, and watcher completions stay pending.
- `ProviderSignInRunner.verify` runs the profile's `login_probe_command`, one
  minimal authenticated request on the execution host. Success marks the account
  `signed_in` and calls `resume_account`, wired in `api/app.py` to
  `resume_provider_account` (`src/rcp/runs/provider_login.py`), which releases
  the parked work once through the existing recovery owners.

## Design

Extend that account owner rather than add a parallel one.

1. **One state owner per account.** `ProviderAccounts` stays the single owner.
   Add a `limited` state beside `signed_in` and `signed_out`, with the same
   generation fence.
2. **Passive observation first.** A turn that fails with `session_limit` marks
   its account `limited`, as `provider_auth` marks it `signed_out` today. No
   provider prose is parsed beyond the existing classification.
3. **Park the same way.** While `limited`, the existing admission fence parks
   paid work for that account exactly as for `signed_out`.
4. **Active probe only while limited.** One background loop in the backend
   process probes each `limited` account with the existing
   `login_probe_command` on a bounded interval (in `limits.py`). One probe per
   account, never per task.
5. **Release through the existing path.** A successful probe marks the account
   `signed_in` and calls the same `resume_account`, so every parked task,
   recovery, mail batch, and watcher completion is released once, idempotently.
6. **Opt-in auto-recover.** With auto-recover off, a successful probe only
   updates the account state the UI shows; parked work waits for the human.
   With it on, release runs automatically.
7. **Projection.** The provider card shows the account state from the same
   owner. Usage numbers are out of scope for now.

The monitor never calls RCP's HTTP API. It lives in the backend process, talks
to the provider only by running its CLI on the execution host, and releases work
through in-process recovery owners. The HTTP routes remain callers of the same
owner (Verify, sign-in, sign-out).

## Open decisions

- Probe interval and whether it backs off.
- Where auto-recover lives in the UI. Candidates: one switch on the provider
  account card (an account fact, set once), or a per-launch choice at the
  entrypoints that spend budget (Experiment and Auto-research start, Agents,
  the chat composer).
- Whether a turn that succeeds while an account is `limited` (another task got
  through) clears the state without a probe.

## Checks when implemented

- Unit: a `session_limit` failure marks the account `limited` and parks new paid
  admission; a successful probe releases each kind of parked work once.
- Integration: a stubbed provider that fails with a limit, then succeeds; the
  episode continues on its saved session without a human Retry when
  auto-recover is on, and waits when it is off.
- Live: a real Claude usage limit on a disposable team server.
