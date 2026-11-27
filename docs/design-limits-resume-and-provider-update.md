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

## 2. agy: quota error and Work launches

agy's quota error (`Individual quota reached … Resets in …`) joins the shared
session-limit markers (section 1, step 2).

Work and orchestrator turns drop `--agent`, because agy bills a custom-agent request at
a higher rate, and run agy's own tool set. That set includes a scheduler and background
agents. The shared watcher prompt tells every agent, on every provider, that a watcher
is its only way to be woken later. Discuss and the artifact edit keep their staged
agent.

## 3. In-app provider update

Current behavior lives in
[Server and machine operations](specs/server-and-machine-operations.md#in-app-provider-updates).

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
