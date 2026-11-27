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

1. **Reset time, read at the failure and kept on the turn.** `settle_provider_login`,
   when `_failure_classified_as(record, "session_limit")`, writes a
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
   3. Otherwise `failed_at + PROVIDER_LOGIN_LIMIT_HINT_SECONDS`, with `estimated: true`.
      The row then reads "Resumes later" with no clock time.
   A reset that is not at least `LIMIT_RESET_MIN_SECONDS` (60, in `limits.py`) after
   the failure counts as unknown and falls to source 3. The readers live in one pure
   module, `providers/usage_limits.py`: `limit_reset(provider, snapshot, error, *,
   failed_at) -> LimitReset`, with one small reader per provider.
   - **Snapshot route.** Each decoder turns its usage event into an `AgentEvent` of kind
     `rate_limit` holding the raw window data. The task's event loop keeps the last one
     on `AgentTaskExecution.rate_limit` (memory only). `settle_provider_login` reads it
     from there. The raw snapshot is stored only as a diagnostic field of
     `provider_limit_reset`, with no receipt of its own.
   - The task row carries `limit_reset` (`resets_at`, `estimated`, `state`: `waiting`
     or `stopped`) for the web.
2. **Classification.** The Gemini quota wording joins the shared session-limit markers.
3. **Wake on the existing 5-second tick.** There are no watchers, schedules or new timers.
   `after_watcher_poll` (`api/app.py`) already runs `provider_sign_ins.reconcile_recovery()`
   every 5 s. It gains one call, `resume_limited_turns(background)`, in
   `runs/provider_login.py`, wrapped so one turn's error never stops the tick.
   - **Candidates.** One SQL query, like `auth_retry_candidates`, finds failed turns
     that have a `provider_limit_reset` receipt and no continuation. Only member chat
     turns count (no `episode_id`), so episode limit handling is untouched. A turn
     qualifies when all of these hold:
     - It has no later turn in the same chat, and no active turn in that chat.
     - Its `resets_at <= now`.
     - The authorizing member's switch is on.
     - It has no `limit_auto_resume_stopped`.
     - Its newest `limit_auto_resume_deferred`, if any, is at least
       `LIMIT_RESUME_RETRY_SECONDS` (60, in `limits.py`) old.
   - **Resume.** `resume_task_as` (`api/tasks.py`) splits in two:
     - `resume_task(previous, authorized_by, *, catalog, background_tasks,
       experiment_admission, marker=None)` holds today's body without the HTTP parts. It
       re-validates the stored request and skills, takes the chat's worktree recovery
       admission, and raises the original exceptions.
     - The HTTP route keeps write admission and the mapping from exceptions to status
       codes, as today.

     `BackgroundAgentTasks.resume` gains `marker`, a receipt category written on the new
     record when it is created. The tick passes `limit_auto_resume`.

     The tick builds its inputs without a request:
     - **Write admission:** a form of `task_write_admission` that takes no FastAPI
       `Request`. It keeps the single-entry exemption and is modeled on
       `WorkEpisodes._admitted` (`api/work_episodes.py`). That function takes the
       operation lock, runs `require_project_accepts_new_work` and checks
       `is_project_member`, all without a request. A Space chat checks the member's
       space role instead.
     - **`AuthorizedHuman`:** built from the member's current `space_users` row.
     - **Lost access:** a member who is no longer a project member, with no space role,
       or with no display name raises `LimitResumeMemberGone`, a `ValueError`.
   - **Outcomes**, checked in this order:
     1. A continuation is created, or `AgentTaskAlreadyContinued` is raised: done. The
        continuation records `limit_auto_resume`.
     2. Short-lived: `ProviderLoginsLimited`, `AgentTaskAdmissionConflict` and its
        subclasses, `ProjectActiveTaskConflict`, `OSError`, `StateUnavailable`. Append a
        `limit_auto_resume_deferred` receipt with the reason, and try again after
        `LIMIT_RESUME_RETRY_SECONDS`. At `LIMIT_RESUME_DEFER_MAX` (60, about an hour)
        deferrals the turn is stopped, so a turn holds at most 60 of these receipts.
     3. Anything else is stopped with its reason: `ProviderSignedOut`,
        `MachineDependenciesMissing`, `LimitResumeMemberGone`, any other `ValueError`, or
        no native session. This writes `limit_auto_resume_stopped`.

     A crash between the check and the resume leaves no receipt. The next tick simply
     tries again.
   - **Loop stop.** The turn and its two nearest ancestors are walked by
     `parent_operation_id`. If all three carry `limit_auto_resume`, the turn gets
     `limit_auto_resume_stopped` instead. The cap is `LIMIT_AUTO_RESUME_MAX` (3, in
     `limits.py`).
   - **Old data and restart.** Rows written before this release have no
     `provider_limit_reset`, so nothing old resumes. A restart needs nothing extra: the
     first tick runs after startup reconciliation and sees resets already past.
4. **Member switch.** A column on `space_users`, `auto_resume_after_limits INTEGER
   NOT NULL DEFAULT 1`, added in the `storage/base.py` migrations. A personal space
   uses its `local_owner` row. API, in `api/provider_login.py` beside the other
   per-member provider settings: `GET`/`PUT /api/providers/limit-resume` with
   `{enabled: bool}` for the signed-in member.
5. **Per-turn opt-out.** "Don't resume" posts to
   `/api/projects/{project_id}/tasks/{operation_id}/limit-resume/decline`, with the
   usual `/api/agent-threads/...` twin. It writes `limit_auto_resume_stopped` with
   `reason: "declined"`. Only the
   turn's authorizing member may decline. To continue after that, the member uses the
   existing Resume button.

### Surfaces (mockups approved 2026-11-27)

- **Chat ⋯ menu:** a "Resume after usage limits" switch below "Browser use". It is the
  member's own setting and reads the same in every chat.
- **Limit row** (the existing paused-turn row): one line with its action at the end.
  - Waiting: `Codex usage limit. Resumes at 4:10 PM.` with **Don't resume**.
  - Switch off, declined, or stopped: `Codex usage limit until 4:10 PM.` with the
    existing Resume and Retry. A stopped turn shows its reason on hover.
- **Sign-in card:** a limited card reads `Limit until 4:10 PM` instead of `Usage limit`.

## 2. Gemini 4x quota drain

Google bills custom-agent requests (`--agent`) at 4x (antigravity-cli #1890, confirmed
2026-11-27). RCP passes `--agent rcp-<hash>` on every agy turn
(`providers/agy/profile.py` `_command` and `work_like_probe_command`). The agent file
carries a tool allowlist (`_tools`).

**Status: on hold until the billing is confirmed.** The probe so far:
- Without `--agent`, agy offers 60 tools: browser control, `schedule`, `send_message`,
  `manage_inbox`, `generate_image`, MCP and more. `--settings` cannot remove a tool. It
  only gates the actions `command`, `read_file`, `write_file`, `mcp`, `read_url` and
  `execute_url`.
- On agy's Claude models, the same short turn cost about the same with and without
  `--agent` (0.78% vs 0.93% of the 5-hour bucket, matching its token count). There was
  no 4x.
- The Gemini comparison runs when that bucket resets.

This PR ships only the quota-error classifier (section 1). The `--agent` decision follows
the Gemini result. If Gemini shows 4x, the agy launch change gets its own design. It
first needs a turn showing that `schedule` and `send_message` are refused without
`--agent`.

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

- **One update body, two targets.** `UpdateTarget` replaces the `(account, runner)` pair
  in `server_ops/provider_update.py`. It has:
  - `home`
  - `run(argv, timeout)`
  - `discover(provider) -> str | None`, which replaces the local `_discover_provider`
  - `temp_dir()`, which creates and safety-checks `~/.rcp/tmp/rcp-provider-*`
  - `remove(path)`

  `binary` becomes a target-side `str`, and the home rule compares it to `target.home`.
  There are two targets:
  - **Local account:** today's `_run_as_account`, `_account_temp_dir` (with its chown,
    chmod and owner checks) and `shutil.rmtree`.
  - **Remote machine:** runs over the machine's SSH transport as its `os_account`.
    - `home` is the machine's `$HOME`.
    - `discover` runs `command -v` plus the profile's `install_paths`.
    - `temp_dir` runs `mktemp -d` and then checks owner and mode with `stat`.
    - `remove` runs `rm -rf` on that directory only.

    Profile data (`native_update`, `installer_url`, `command_prefix`) stays on the
    backend, which builds each argv. Only plain commands cross SSH, so no `rcp.*` code
    ships.
- `update_provider(store, launcher, provider, machine_id, *, target, stop, guard)`
  holds the per-provider body of `auto_update_providers`. The daily pass calls it for
  the backend machine. The endpoint calls it on a worker thread, the way a sign-in
  attempt runs.
- **Hold.** `launcher.provider_update` becomes a try-acquire keyed by
  `(provider, machine_id)`. A second acquire raises `ProviderUpdateRefused`
  ("already updating"), which the API returns as 409. While the hold is taken:
  - Launches wait, remote ones too. The wait moves to the start of the launch, before
    readiness, credential resolution, argv building and `remote_process_start`. A
    waiting turn then holds no credential and announces no remote pass, and it launches
    with the updated binary. The wait is async, as today, and the `not host` exemption
    goes.
  - Readiness and login probes do not wait, and `refresh` is ignored. They return the
    cached readiness marked `updating`. With nothing cached, they return a minimal
    `ProviderReadiness` with `updating` set and authentication unknown. The row's
    **Updating** tag reads this.
  - Sign-ins are refused with "Updating".
  - The updater's own `run` calls do not check the hold.
- **Busy rule.** `backend_turns_using` becomes `turns_using(store, provider,
  machine_id)` and is checked inside the hold, as the daily pass does today. If turns
  are running on that provider and machine, the on-demand update
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
  - Reader tests per provider, using captured output: the Gemini quota error
    (`Individual quota reached … Resets in 2h57m1s.`), Codex `rateLimits/updated` and
    `usageLimitExceeded`, and Claude `rate_limit_event`.
  - Wake tests on the test clock:
    - A limited chat turn resumes at its reset, exactly once, across a restart.
    - It still resumes after another turn's success clears the login's mark.
    - `ProviderLoginsLimited` from `resume` is deferred and retried.
    - `ProviderSignedOut` and a lost membership stop the turn.
    - Deferrals stop at their cap.
    - A fourth limit in a row stops.
    - It stays failed when the switch is off or "Don't resume" was pressed.
    - An episode turn is untouched.
    - An old limited turn with no `provider_limit_reset` is untouched.
  - Old-data replay of `provider_login_assignments` and `space_users`.
- **Update:**
  - Local and remote (stage double) for success, failure, outside-home refusal and the
    busy refusal.
  - A permission refusal.
  - The daily pass and on-demand job meet at one lock; a remote launch waits on it.
- **Web:** browser tests at 1440 and 390 px for the provider menu, the chat-menu switch,
  the limit row and the sign-in card.
