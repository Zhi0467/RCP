# Design: resume after usage limits, Gemini quota drain, in-app provider update

This design ships in one PR for release 0.4.55. Before merge its rules move into the
owning specs and this file is deleted.

## 1. Resume after usage limits

### Today

- `classify_terminal_error` (`providers/__init__.py`) matches limit wording in the
  error text. It misses Gemini's `Individual quota reached ... Resets in 3h27m0s`
  (`RESOURCE_EXHAUSTED`), so an agy limit counts as an ordinary failure.
- `settle_provider_login` marks the login limited (`limited_at`). The mark lasts a
  guessed `PROVIDER_LOGIN_LIMIT_HINT_SECONDS` (5 hours).
- After a sign-in, `auth_retry_candidates` and `resume_provider_account` retry failed
  `provider_auth` turns, including plain Work chats. Nothing does the same when a
  limit ends. Episode turns can move to another login (`provider_login_limit_retry`),
  but a member chat that hits a limit waits until someone presses Resume.

### Change: extend the sign-in wake to the limit reset

1. **Reset time.** A limited login records `limit_resets_at` next to `limited_at`.
   The first source that answers wins:
   1. Provider usage data:
      - Codex: app-server `account/rateLimits/read`, plus the
        `account/rateLimits/updated` events RCP drops today
        (`resetsAt` on the exhausted window).
      - Claude: the stream's `rate_limit_event` (`unifiedWindows.*.resetsAt`).
      - Gemini: `agy -p /usage --output-format json`. RCP already runs it as the
        sign-in probe and discards the data. Use the earliest `reset_time` of the
        exhausted bucket.
   2. Error text, when it names a time: Gemini's `Resets in 3h27m0s` and Codex's
      `try again at ...`.
   3. The existing 5-hour guess. The turn row shows no exact time in this case
      ("Resumes later today").
   Each provider profile owns its reader beside its error classifier.
   `limit_resets_at` replaces the guess in `provider_login_limit_lapses_in`.
2. **Classification.** Add the Gemini quota wording to the session-limit markers.
3. **Wake.** At `limit_resets_at` the backend calls `resume_provider_account` for that
   login, the same call a sign-in makes. Candidates widen from `provider_auth`
   failures to `session_limit` failures too (`limit_retry_candidates`, the
   sibling of `auth_retry_candidates`). The same "a later turn in this chat makes
   this one final" rule applies. A limit failure keeps its native session, so the
   wake uses **Resume**, not Retry.
   One timer per login lives in the backend loop. Startup recovery re-arms the timers
   from stored `limit_resets_at` values, so a restart loses nothing.
4. **Member switch.** A new `member_preferences` table (`user_id` primary key,
   `auto_resume_after_limits` default 1), beside `space_users`. A missing row means on.
   A personal space uses its one local user id.
   The wake only resumes turns whose authorizing member has it on. Episode and
   Auto-research recovery keep their current behavior; the switch covers member
   chat turns.
5. **Per-turn opt-out.** "Don't resume" on a waiting turn records an
   `auto_resume_declined` receipt. `limit_retry_candidates` skips turns that carry it.

### Surfaces (mockups approved 2026-11-27)

- **Chat ⋯ menu:** a "Resume after usage limits" switch below "Browser use". It is
  the member's own setting and reads the same in every chat.
- **Limit row** (the existing paused-turn row): one line with its action at the end.
  - Waiting: `Codex usage limit. Resumes at 4:10 PM.` with **Don't resume**.
  - Off or declined: `Codex usage limit until 4:10 PM.` with **Resume at 4:10 PM**
    (re-arms the wake for this turn) and the Retry icon.
- **Sign-in card:** a limited card reads `Limit until 4:10 PM` instead of `Usage limit`.
- **Space Running list:** "Session limit" becomes "Resumes 4:10 PM" while a wake is
  armed.

## 2. Gemini 4x quota drain

Google bills custom-agent requests (`--agent`) at 4x (antigravity-cli #1890, confirmed
2026-11-27). RCP passes `--agent rcp-<hash>` on every agy turn
(`providers/agy/profile.py`, `remote.py`). The agent file only narrows tools for the
non-Work capabilities.

The fix removes those capabilities rather than working around them:

- **Artifact-comment edits** (`runs/tasks/artifact_edit.py`, the "revoking" launch)
  become ordinary Work turns in the artifact's chat. The Review/Accept mode decides
  how edits land, as everywhere else.
- **Provider-switch summaries** (`runs/agent_threads.py`, `session_summary`) run as
  Work turns.
- The `discuss` and `session_summary` capabilities, their `permissions_for` branches,
  and agy's `_tools` split are deleted. Historical Discuss turns still render. Their
  stored `mode` stays readable, and no new launch uses it.
- agy launches drop `--agent` and the per-capability agent file entirely.

Check: a real agy Work turn before and after, comparing the `/usage` bucket drop for the
same prompt. This spends a little real quota and needs a go-ahead first.

## 3. In-app provider update

### Today

`server_ops/provider_update.py` updates a provider CLI with
Inspect → Update → Verify. `runs/provider_auto_update.py` runs it daily, but only on
the backend machine and only for binaries inside the account's home. Remote machines
and on-demand updates need an operator in a terminal. `launcher.provider_update`
already holds new launches while an update runs.

### Change

- `POST /api/space/machines/{machine_id}/providers/{provider}/update` starts an
  update job and returns at once. It is modeled on the sign-in attempt
  (`api/provider_login.py`, `runs/provider_sign_in.py`): one live job per machine
  and provider, polled by the card.
- Backend machine: the existing `update_installed_provider`. Remote machine: the same
  module shipped over SSH as the machine's `os_account` (shipped from its source
  module, per AGENTS.md), with the same home-directory rule. A binary outside the
  account's home fails visibly with the reason (for example, it needs `sudo npm -g`).
- Success clears `update_warning` and re-resolves the path. Failure sets it, so the
  row shows the existing **Update failed** tag with the error on hover.

### Surface (mockups approved 2026-11-27)

The provider row replaces the **Resolve** button with a ⋯ menu holding **Resolve**
and **Update**. While an update runs, the tag reads **Updating** and the ⋯ becomes a
spinner. On a phone the name, tag and ⋯ share one line and the path field is full
width below.

## Spec owners

- Limits, wake, and switch: `docs/specs/providers-and-containment.md` (login limits)
  and `conversations-episodes-and-watchers.md` (turn recovery).
- Capability removal: `providers-and-containment.md`, and invariant 4's wording in
  `AGENTS.md` (Discuss no longer exists).
- Update action: `docs/specs/server-and-machine-operations.md`.
- UI: `interface-and-visual-design.md`.

## Tests

- **Limits:** classifier fixtures from captured provider output (the Gemini quota
  error, Codex `usageLimitExceeded`, Claude `rate_limit_event`). A reset-time
  reader test per provider. A wake test on the test clock: a limited chat turn
  resumes at the reset, and a turn with the switch off or "Don't resume" stays.
  A restart re-arms the timer.
- **Capabilities:** artifact-comment edit and provider-switch summary run as Work
  turns. Old Discuss records still replay and render.
- **agy:** the launch argv has no `--agent`.
- **Update:** a job test for success, failure and outside-home refusal, local and
  remote with a stage double.
- **Web:** browser tests at 1440 and 390 px for the row, menu, switch and limit row.
