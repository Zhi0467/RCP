# Design: resume after usage limits, Gemini quota drain, in-app provider update

This design ships in one PR for release 0.4.55. Before merge its rules move into the
owning specs and this file is deleted.

## 1. Resume after usage limits

### Today

- `classify_terminal_error` (`providers/__init__.py`) is shared text matching. It
  misses Gemini's `Individual quota reached ... Resets in 3h27m0s`
  (`RESOURCE_EXHAUSTED`). A limit is not a `failure_kind`. It shows up as a
  `provider_terminal_error` receipt with `classification: session_limit`, or as a
  text match (`BackgroundAgentTasks._failure_classified_as`).
- `settle_provider_login` marks the login limited (`provider_login_assignments.limited_at`)
  through `set_provider_login_limit`. The mark lasts a guessed
  `PROVIDER_LOGIN_LIMIT_HINT_SECONDS` (5 hours, `provider_login_limit_lapses_in`).
  Only a later successful turn clears it, and `provider_login_limited(continued=True)`
  ignores the timeout.
- Automatic wakes (watchers, schedules) already wait out a limit. Admission refuses
  them with `ProviderLoginsLimited` until the mark lapses, and their pollers offer them
  again every 5 s (watchers) or every 60 s (schedules). Episode turns move to another
  login (`provider_login_limit_retry`).
- **The gap:** a member's chat turn that fails on a limit stays failed until someone
  presses Resume. `auth_retry_candidates` wakes `provider_auth` failures after a sign-in.
  Nothing wakes limit failures when the limit ends.

### Change

1. **Reset time, read at the failure and kept on the turn.** Settlement writes a
   `provider_limit_reset` receipt on the failed turn: `{resets_at, estimated}`. The
   login keeps a copy in a new `provider_login_assignments.limit_resets_at` column, added
   in the `provider_logins.py` table setup. The turn's copy drives the wake. The login's
   copy drives `provider_login_limit_lapses_in`, which then also ends the
   `continued` case. A later success on the login, or a re-sign-in, clears the login's
   copy but not the turn's. The first source that answers wins:
   1. Usage data the turn already produced, kept as the turn's last snapshot only:
      - Codex: the `account/rateLimits/updated` events that
        `providers/codex/app_server.py` drops today (`resetsAt` on the exhausted window).
      - Claude: the stream's `rate_limit_event` (`unifiedWindows.*.resetsAt`).
   2. Error text that names a time: Gemini `Resets in 3h27m0s`, Codex `try again at …`.
      Gemini's quota error always carries it, so no extra `/usage` launch runs during
      settlement.
   3. Otherwise `limited_at + PROVIDER_LOGIN_LIMIT_HINT_SECONDS`, with `estimated: true`.
      The row then reads "Resumes later" with no clock time.
   A reset that is not at least one minute after the failure counts as unknown and falls
   to source 3. The readers live in one pure module, `providers/usage_limits.py`:
   `limit_reset(provider, snapshot, error, *, failed_at) -> LimitReset`, with one small
   reader per provider. The snapshot reaches it as a `provider_rate_limit` receipt the
   stream decoder writes. The task row carries `limit_reset` (`resets_at`, `estimated`)
   for the web.
2. **Classification.** The Gemini quota wording joins the shared session-limit markers.
3. **Wake on the existing 5-second tick.** There are no watchers, schedules or new timers.
   `after_watcher_poll` (`api/app.py`) already runs `provider_sign_ins.reconcile_recovery()`
   every 5 s. It gains one call, `resume_limited_turns(background)`, in
   `runs/provider_login.py`, wrapped so one turn's error never stops the tick. A turn
   qualifies when all of these hold:
   - It failed with a `session_limit` classification.
   - It is a member chat turn (no `episode_id`, so episode limit handling is
     untouched).
   - It has `can_resume`, a native session, and no later turn in the same chat (the
     `auth_retry_candidates` rule).
   - Its `provider_limit_reset.resets_at <= now`.
   - The authorizing member's switch is on, and the turn has no `limit_resume_declined`.
   - It has no live `limit_auto_resume` claim. A claim is live unless a later
     `limit_auto_resume_deferred` receipt follows it.
   - Fewer than 3 `limit_auto_resume` claims sit on its continuation chain. This stops a
     wrong reset time from looping.

   The tick writes the claim, then calls
   `background.resume(operation_id, authorized_by=task.authorized_by)`. If `resume`
   raises, the tick writes `limit_auto_resume_deferred` and the next tick tries again.
   Rows written before this release have no `provider_limit_reset`, so nothing old
   resumes. A restart needs nothing extra. The first tick runs after startup
   reconciliation and sees resets that are already past.
4. **Member switch.** A column on `space_users`, `auto_resume_after_limits INTEGER
   NOT NULL DEFAULT 1`, added in the `storage/base.py` migrations. A personal space
   uses its `local_owner` row. API, in `api/provider_login.py` beside the other
   per-member provider settings: `GET`/`PUT /api/providers/limit-resume` with
   `{enabled: bool}` for the signed-in member.
5. **Per-turn opt-out.** "Don't resume" posts to
   `/api/projects/{project_id}/tasks/{operation_id}/limit-resume/decline`, with the
   usual `/api/agent-threads/...` twin, and writes `limit_resume_declined`. Only the
   turn's authorizing member may decline. To continue after that, the member uses the
   existing Resume button.

### Surfaces (mockups approved 2026-11-27)

- **Chat ⋯ menu:** a "Resume after usage limits" switch below "Browser use". It is the
  member's own setting and reads the same in every chat.
- **Limit row** (the existing paused-turn row): one line with its action at the end.
  - Waiting: `Codex usage limit. Resumes at 4:10 PM.` with **Don't resume**.
  - Switch off, or declined: `Codex usage limit until 4:10 PM.` with the existing
    Resume and Retry.
- **Sign-in card:** a limited card reads `Limit until 4:10 PM` instead of `Usage limit`.

## 2. Gemini 4x quota drain

Google bills custom-agent requests (`--agent`) at 4x (antigravity-cli #1890, confirmed
2026-11-27). RCP passes `--agent rcp-<hash>` on every agy turn
(`providers/agy/profile.py` `_command` and `work_like_probe_command`). The agent file
carries a tool allowlist (`_tools`).

Change, for Work and orchestrator turns only, which are nearly all agy traffic:

- `_command` and `work_like_probe_command` drop `--agent` and `--rcp-agy-tools` for
  these capabilities. The `--settings` permissions already carry the write roots, the
  read denies and `command(*)`.
- In `providers/agy/remote.py`, `_agy_host_policy` runs whenever `--settings` is
  present, which adds the temp-dir, provider-homes and browser-shim denies. Its
  `--agent` rename and `_agy_stage_agent` run only when `--agent` is present.
- The read-only launch (artifact-comment edits) keeps `--agent` and its narrower tools.
  It is rare. agy already refuses `session_summary`. Removing the read-only capability
  is a separate design.

**Probe before building (approved 2026-11-27).** Run real agy turns on throwaway data:
1. A Work turn without `--agent`. List the tools it offers. `--settings` rules work by
   action (`read_file`, `write_file`, `command`, `read_url`), so any tool outside the Work
   allowlist must be shown to be blocked another way (for example, the browser shims).
   If one cannot be blocked, Work keeps one shared, stable agent name instead, and the
   probe checks whether that still bills 4x.
2. Resume a session started with `--agent`, now without it. Check its tools.
3. Compare the `/usage` bucket drop for the same prompt with and without `--agent`.

## 3. In-app provider update

### Today

`server_ops/provider_update.py` updates a provider CLI with Inspect → Update → Verify.
`runs/provider_auto_update.py` (`auto_update_providers`) runs it daily on the backend
machine, for binaries inside the account's home:
- It defers while turns use the provider.
- It holds new launches (`launcher.provider_update(provider)`, keyed by provider and
  local launches only).
- It sets `update_warning` in memory, then refreshes readiness.

Remote machines and on-demand updates need an operator in a terminal.

### Change

- **One update body, two targets.** `update_installed_provider` already takes a
  `runner`. It gains an `UpdateTarget` with `run(argv, timeout)`, `temp_dir()` and
  `home`. `_update_provider` stages the installer in `target.temp_dir()` instead of the
  local `~/.rcp/tmp`. There are two targets:
  - The local account: today's `_run_as_account` and `_account_temp_dir`.
  - A remote machine: runs over the machine's SSH transport as its `os_account`. It
    resolves `$HOME` and `mktemp -d ~/.rcp/tmp/...` on the machine. Profile data
    (`native_update`, `installer_url`, `command_prefix`) stays on the backend, which
    builds each argv. Only plain commands cross SSH, so no `rcp.*` code ships.
- `update_provider(store, launcher, provider, machine_id, *, target, stop, guard)`
  holds the per-provider body of `auto_update_providers`. The daily pass calls it for
  the backend machine. The endpoint calls it on a worker thread, the way a sign-in
  attempt runs.
- **Hold.** `launcher.provider_update` becomes a try-acquire lock keyed by
  `(provider, machine_id)` that refuses a second holder. Every spawn of that binary
  waits while it is held: local and remote launches, readiness and login probes, and
  sign-ins. `backend_turns_using` takes the machine and counts turns there.
- **Busy rule.** If turns are running on that provider and machine, the on-demand update
  is refused with the count ("2 turns are using Codex"). It does not wait. A daily pass
  and an on-demand job for the same pair meet at the lock, and the second is refused.
- The home rule is unchanged. A binary outside the account's home fails with the reason
  (for example, it needs `sudo npm -g`).
- `POST /api/space/machines/{machine_id}/providers/{provider}/update` starts the job.
  `GET …/update` returns its state. One live job per pair, held in memory like a sign-in
  attempt (`runs/provider_sign_in.py`). Who may call it: in a team space, a member who
  may edit that machine card. In a personal space, the owner.
- Success clears `update_warning` and re-resolves the path. Failure sets it, so the row
  shows **Update failed** with the error on hover. The warning stays in memory, as today.

### Surface (mockups approved 2026-11-27)

The provider row replaces **Resolve** with a ⋯ menu holding **Resolve** and **Update**.
While an update runs, the tag reads **Updating** and the ⋯ becomes a spinner. On a phone
the name, tag and ⋯ share one line, and the path field is full width below.

## Spec owners

- Limits, wake and switch: `docs/specs/providers-and-containment.md` (login limits)
  and `conversations-episodes-and-watchers.md` (turn recovery).
- agy launch: `providers-and-containment.md` (Antigravity).
- Update action: `docs/specs/server-and-machine-operations.md`.
- UI: `interface-and-visual-design.md`.

## Tests

- **Limits:**
  - Reader tests per provider, using captured output: the Gemini quota error and
    `/usage` JSON, Codex `rateLimits/updated` and `usageLimitExceeded`, and Claude
    `rate_limit_event`.
  - Wake tests on the test clock:
    - A limited chat turn resumes at its reset, exactly once, across a restart.
    - It still resumes after another turn's success clears the login's mark.
    - A `resume` that raises is deferred and retried; a fourth limit in a row stops.
    - It stays failed when the switch is off or "Don't resume" was pressed.
    - An episode turn is untouched.
    - An old limited turn with no `provider_limit_reset` is untouched.
  - Old-data replay of `provider_login_assignments` and `space_users`.
- **agy:**
  - Work argv and the work-like probe have no `--agent`.
  - The host deny rules are still applied.
  - The read-only launch still stages its agent.
- **Update:**
  - Local and remote (stage double) for success, failure, outside-home refusal and the
    busy refusal.
  - A permission refusal.
  - The daily pass and on-demand job meet at one lock; a remote launch waits on it.
- **Web:** browser tests at 1440 and 390 px for the provider menu, the chat-menu switch,
  the limit row and the sign-in card.
