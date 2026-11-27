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

1. **Reset time, read once at the failure.** `settle_provider_login` stores
   `limit_resets_at` (a new column on `provider_login_assignments`, added in the
   `provider_logins.py` table setup) beside `limited_at`. The first source that
   answers wins:
   1. Usage data the turn already produced:
      - Codex: the `account/rateLimits/updated` events that `providers/codex/app_server.py`
        drops today (`resetsAt` on the exhausted window).
      - Claude: the stream's `rate_limit_event` (`unifiedWindows.*.resetsAt`).
      Both are kept on the turn as a `provider_rate_limit` receipt.
   2. Gemini: the usage query RCP already runs as its sign-in probe
      (`agy -p /usage --output-format json`), run once at the failure. The reset is the
      earliest `reset_time` of an exhausted bucket.
   3. Error text that names a time: Gemini `Resets in 3h27m0s`, Codex `try again at …`.
   4. The 5-hour guess. The row then reads "Resumes later" with no clock time.

   Sources 1–3 live in one module, `providers/usage_limits.py`. It exposes
   `limit_resets_at(provider, receipts, error, *, now) -> datetime | None` and keeps one
   small reader per provider, the way each profile keeps its own stream decoding.
   `provider_login_limit_lapses_in` uses `limit_resets_at` when it is set. A lapsed
   reset also ends the `continued` case. `set_provider_login_limit(limited=False)`
   clears both columns.
2. **Classification.** The Gemini quota wording joins the shared session-limit markers.
3. **Wake on the existing 5-second tick.** There are no watchers, schedules or new timers.
   `after_watcher_poll` (`api/app.py`) already runs `provider_sign_ins.reconcile_recovery()`
   every 5 s. It gains one call, `resume_limited_turns(background)`, in
   `runs/provider_login.py`:
   - Candidates are failed turns with a `session_limit` classification that are member
     chat turns (no `episode_id`, so episode limit handling is untouched). They also
     need `can_resume` and a native session, and no later turn in the same chat, which
     is the rule `auth_retry_candidates` uses.
   - The turn's login has `limit_resets_at <= now`.
   - The authorizing member's switch is on, and the turn carries no
     `limit_resume_declined` receipt.
   - The turn carries no `limit_auto_resume` claim. The claim is written in the same
     transaction that selects the turn, modeled on `_claim_auth_retry` but in its own
     category. One claim per failed turn, so a later limit in the same chat can wake
     again.
   - Then `background.resume(operation_id)`. A turn that cannot resume is left as it is.
   - Only turns whose login has a `limit_resets_at` qualify. Rows written before this
     release have none, so the first tick after the update resumes nothing old.
   A restart needs nothing extra. The first tick after startup sees resets that are
   already past. The tick runs only after startup reconciliation, because `resume()`
   refuses before then.
4. **Member switch.** A column on `space_users`, `auto_resume_after_limits INTEGER
   NOT NULL DEFAULT 1`. A personal space uses its `local_owner` row. API:
   `GET`/`PUT /api/me/preferences` with `{auto_resume_after_limits: bool}`.
5. **Per-turn opt-out.** "Don't resume" posts to
   `/api/projects/{id}/agent-tasks/{op}/limit-resume/decline`, which writes
   `limit_resume_declined`. To continue after that, the member uses the existing
   Resume button.

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

- Drop `--agent` and the staged agent file. The `--settings` permissions already carry
  the write roots, the read denies and `command(*)`.
- In `providers/agy/remote.py`, the host policy (temp-dir, provider-homes and
  browser-shim denies) keys off `--settings`, not off `--rcp-agy-tools`.
  `_agy_stage_agent` runs only when `--agent` is present.
- The read-only launch (artifact-comment edits) keeps `--agent` and its narrower tools.
  It is rare. agy already refuses `session_summary`. Removing the read-only capability
  is a separate design.

**Probe before building (needs a go-ahead, spends a little real quota):** run one agy
Work turn without `--agent`. Then:
1. List the tools agy offers by default. Any browser or tool outside the Work allowlist
   must be denied in `--settings`, or this change does not ship.
2. Compare the `/usage` bucket drop against the same prompt with `--agent`.

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

- Pull the per-provider body of `auto_update_providers` out into
  `update_provider(launcher, store, provider, machine_id)`. The daily pass and the new
  endpoint both call it.
- The hold and the busy check are keyed by `(provider, machine_id)`.
  `backend_turns_using` takes the machine.
- On-demand rule: if turns are running on that provider and machine, the update is
  refused with the count ("2 turns are using Codex"). It does not wait. A daily pass
  and an on-demand job for the same pair share the hold, so the second one is refused
  as busy.
- Remote machine: the update helper is shipped from its source module over SSH and run
  as the machine's `os_account`. It imports nothing from `rcp.*`. The same
  home-directory rule applies. A binary outside the account's home fails with the
  reason (for example, it needs `sudo npm -g`).
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
    - It stays failed when the switch is off or "Don't resume" was pressed.
    - An episode turn is untouched.
    - An old limited row with no `limit_resets_at` is untouched.
  - Old-data replay of `provider_login_assignments` and `space_users`.
- **agy:**
  - Work argv has no `--agent`.
  - The host deny rules are still applied.
  - The read-only launch still stages its agent.
- **Update:**
  - Local and remote (stage double) for success, failure, outside-home refusal and the
    busy refusal.
  - A permission refusal.
  - The daily pass and on-demand job share one hold.
- **Web:** browser tests at 1440 and 390 px for the provider menu, the chat-menu switch,
  the limit row and the sign-in card.
