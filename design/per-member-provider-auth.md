# Per-member provider sign-in

Status: design for review. No code yet. Before it ships, its rules move into
`docs/specs/providers-and-containment.md` and this file is deleted.

## Gist

Today each machine has exactly one Claude sign-in and one Codex sign-in, shared
by the whole team. With this change:

- Each machine has a **pool** of sign-ins per provider. Any member with access to
  the machine can add one to the pool or remove one.
- Each member can also add **their own** sign-in on a machine.
- Each member sets, per machine and provider, what their work uses: **Mine**,
  **Pool**, **Mine first** or **Pool first**. It is a setting, not a per-run
  choice. RCP applies it to every chat, episode and background turn.
- Within the pool, a session stays on the same sign-in while it is healthy. A new
  session takes the healthy one used longest ago, which spreads load across
  accounts.
- If a sign-in dies, a Claude session moves to another allowed sign-in at the
  next turn, and RCP records the move. A Codex session waits for its own sign-in,
  because Codex keeps the session inside that sign-in's folder.
- Everyone can see whether each sign-in is healthy. Nobody sees anyone else's
  tokens or sign-in codes.

**What happens to your team**

| Situation | Result |
| --- | --- |
| Nobody changes anything | Same as today. Each machine's pool holds its one existing sign-in. |
| A member adds a second account to the pool | New sessions spread across both. Running sessions stay where they are. |
| Everyone uses their own | Set "Mine" for each member. Each member's work bills their own account. |
| Some use their own, some use the pool | Each member's setting decides. |
| Someone joins after the upgrade | Their work is refused until they add their own sign-in or set "Pool" or "Pool first". |
| A pool sign-in is signed out or revoked | Claude sessions on it move at their next turn. Codex sessions on it wait for it. New sessions skip it. |
| You join a teammate's Codex chat | It stays on the sign-in it started with, so that account pays. In a Claude chat your own setting decides. |
| That sign-in's owner is removed from the space | Codex chats bound to their own sign-in stop; start a new chat. Pool sign-ins they added stay. |

Machines that run no agents never consult the pool. Nothing changes for them.

There is no team or owner default. Without a saved setting, a team member's
setting is **Mine**, so using the pool is each member's own opt-in. In a personal
space, the setting is fixed to **Pool**, since one person owns every sign-in.
Members who exist at upgrade get **Mine first**, so nothing changes for them.

The spec says RCP creates no extra provider homes. This changes that: each added
Codex sign-in gets its own folder. RCP still never moves a running Codex session
from one account to another.

## Terms

| Term | Meaning |
| --- | --- |
| Host | The machine the agent runs on. Empty means the RCP machine itself. |
| Login | One provider sign-in on one host, keyed `(provider, host, login_id)`. |
| Machine's own login | `login_id = ""`. Today's only login: the OS account's own CLI login for Codex and OpenCode, and the stored token for Claude. It cannot be removed, only signed out. |
| Pool | The machine's own login plus every login a member added to the machine. `member_id` is empty. Any member may sign in, verify, sign out, add or remove pool logins. |
| Member login | A login with `member_id` set. One per member, provider and host. Only that member may act on it. UI row: "Yours". |
| Setting | Per `(member, provider, host)`: `mine`, `pool`, `mine_first` or `pool_first` (UI: "Mine", "Pool", "Mine first", "Pool first"). |
| Healthy | The credential is present, the row is `signed_in`, and the login is not removed. |
| Bound | A Codex session stays on the login it started with, because the session lives in that login's home folder. |
| Turn boundary | The start of a new agent task, except a transport reattempt (`retry` admitted by the transport timer) or a `graph_repair` correction, which inherit their parent's login. |
| Previous login | For a continued session: the `login_id` of the latest task row with the same provider, host and `native_session_id` that reached provider start, other than the current task. A fresh session has none. |
| Park | Work waits, without failing, until its login is healthy again. |
| Fence, generation | As in the providers spec, [Login state and failure](../docs/specs/providers-and-containment.md#login-state-and-failure). |

## 1. Data model and migration

One storage schema migration, numbered after `consolidation_targets_v1`:

- `provider_login_states`: rebuilt with key `(provider, host, login_id)`.
  `login_id TEXT NOT NULL DEFAULT ''`. Old rows keep `login_id = ''` and become
  each machine's own login. New columns:
  - `member_id TEXT` (`NULL` for pool logins), with a unique index on
    `(provider, host, member_id)` where it is set;
  - `added_by`, `added_at`;
  - `draining_since`, set when a member removes a pool login that still has
    bound work (section 3);
  - `removed_at`, which keeps a removed login's row for usage history.
- New `provider_login_assignments(provider, host, login_id, last_assigned_at)`,
  the pool's least-recently-used clock. It is a separate table so that writing
  it on every launch does not fire the resource-version triggers that make
  Settings refetch.
- `provider_readiness_snapshots`: a cache. Drop it and recreate it keyed
  `(provider, host, login_id, binary)`.
- New `provider_login_settings(user_id, provider, host, setting, changed_at)`.
  `setting` is a TEXT enum of the four values. `host = '*'` is a member's
  fallback for any host without its own row. Only the migration writes `*` rows
  (below). Lookup order: the host's row, then the member's `*` row, then the
  default (`mine` in a team space, `pool` in a personal one).
- `graph_runs` (task rows): add `login_id TEXT`. `NULL` means "not resolved yet".
  `''` means the machine's own login.

There is no episode-level table. The binding that matters is per session
(section 3).

The migration also:

- sets `login_id = ''` on every existing task row, so all work from before the
  upgrade reads as machine work, and `NULL` keeps one meaning;
- writes a `*` row with `mine_first` for every current team member
  (`identity_kind = 'team_member'`, `removal_started_at IS NULL`) and every
  provider. This covers hosts that never had a login row and machines added
  later, so nothing changes for existing members anywhere;
- reinstalls the resource-version triggers on the rebuilt tables and registers
  the new one (`storage/resource_versions.py`);
- reinstalls the readiness deadline trigger with `login_id` in its owner key and
  deletes the old deadline rows (`storage/resource_deadlines.py`).

`ProviderLoginStateRecord` gains the new fields. A missing row for a machine's
own login still reads as `signed_in`, generation 0, exactly as today. Other logins
always have a row, because adding one writes it.

Readers of `provider_login_states` skip removed rows: the Settings list, release,
and the resolver. `mark_provider_login_verified` and the sign-in runner refuse a
removed login, so a late verify or token paste cannot revive it.

**Credentials and homes on disk.** `ProviderCredentialStore.account_dir` gains
`login_id`.

- The machine's own login keeps today's paths, so nothing moves.
- Every added login, pool or member, lives under `<account dir>/logins/<login_id>/`.
  Login ids are UUID4s, so they are path-safe.
- All of `providers/` stays out of backups and transfers. Codex sessions inside
  added homes are therefore not backed up either, the same as machine sessions in
  `~/.codex` today.

**Compatibility with old writers** (AGENTS.md, replay and compatibility):

- Canonical graph data does not change. Patches, replay and merges are untouched,
  and agent Patch input has no login field.
- Old login rows become each machine's own login. Old task rows carry
  `login_id = ''`. Old queued and recovery rows without `authorized_by` use the
  machine's own login. Legacy watcher wakes are already terminalized.
- Codex sessions started before the upgrade are bound to the machine's own login,
  which is still the OS home, so they resume as before.
- Old parked work is released by verifying the machine's own login, as today.
- Restore: `detach_provider_logins_for_restore` fences every `signed_in` row,
  including added ones, and carries `login_id` into the rows it rebuilds.
- A second RCP process on the same OS account keeps using the machine Codex home
  and `~/.rcp/credential-locks/<provider>.lock`. The machine's own login keeps
  both, so the two processes still take turns.
- Old versions cannot read the new tables. To roll back, restore the backup taken
  before the update, which is the existing rollback path.

## 2. Resolving a launch

**Which member.** The task's `authorized_by` member:

| Work | Member |
| --- | --- |
| Chat, Work, Discuss, Paper coach | The member who sent the turn. |
| Watcher wake | The origin task's member (`resolve_watcher_delivery_authorizer`). |
| Auto-research orchestrator and children | The episode's `authorized_by`. Children already must match it. |
| Experiment loop turns, wrap-up reports | The episode's `authorized_by`. |
| Nightly consolidation | The schedule's `authorized_by`. |
| Recovery, Retry, transport reattempts | The original task's member. |

**Which login.** One resolver in `agents/provider_accounts.py` returns a
`LoginKey(provider, host, login_id)` or a refusal. Its inputs are the provider,
the host, the member, the continued session's login if any, and the session's
previous login if any. Rules, first match wins:

1. **Same lineage.** A transport reattempt or a `graph_repair` correction keeps
   its parent row's login. If that login is not healthy, the task waits for it.
2. **Bound session (Codex).** A task that continues a Codex session uses that
   session's login: the `login_id` of the earliest task row with the same
   provider and `native_session_id` that reached provider start. A fork has no
   session id until the provider names it, so it uses its fork source session's
   login. Watcher wakes and Paper coach sessions inherit it the same way. If that
   login is unhealthy, the task waits. If it was removed, the task fails with
   `session_refused` and the reason "start a new chat".
3. **Source.** Walk the member's setting over the two sources, mine and pool.
   Take the first source that has a healthy login.
4. **Login within the source.**
   - Mine: the member's login.
   - Pool: the session's previous login, if it is in the pool, healthy and not
     draining. Otherwise the healthy, non-draining pool login with the oldest
     `last_assigned_at`. Ties go to the machine's own login, then the oldest
     `added_at`.
5. **Refuse.** The refusal names the first source in the setting.

OpenCode has no added logins. Its pool is only the machine's own login (section 4).

Rule 4 runs inside the `BEGIN IMMEDIATE` transaction of "When" step 2. That
transaction picks the login, writes the task's `login_id` and updates
`last_assigned_at`, so two simultaneous new sessions land on different logins.
A sticky reuse also updates the clock, so a login carrying a busy session does
not look idle. The first assignment of a machine's own login inserts its rows.

`account_login_refusal` becomes a check on one resolved `LoginKey`. Every caller
goes through the resolver first. The launcher's `stream`, `_login_refusal`,
`process_environment` and readiness cache, the credential gate, and the sign-in
runner's `settle_interrupted_sign_ins` and `reset_logins_without_credentials` all
take the `LoginKey` instead of `(provider, host)`.

**When.**

1. Admission, before task creation and budget debit, resolves without writing.
   If nothing is usable, it refuses with `ProviderSignedOut`, as today.
   `admit_provider_task` gains the authorizer, so `_start` substitutes the
   episode's `authorized_by` before admission instead of after.
2. The launch resolves again in one transaction (above) and writes `login_id` to
   the task row. If that login
   differs from the session's previous login, it also writes a
   `provider_login_switched` receipt with `from`, `to` and a reason:
   `unhealthy`, `setting_changed` or `preferred_source_available`.
3. The launch takes the credential gate for that login and checks it again.
4. If the login went bad while the launch waited:
   - Bound work (rules 1 and 2) fails as `provider_auth` and parks under that
     login. Today this recheck raises a plain `ValueError` with no failure kind,
     so it parks nothing. A typed flag on `AgentTaskExecution`, beside
     `launch_refused`, records the refusal and its `LoginKey`. `_failure_kind`
     maps the flag to `provider_auth`, never by matching the refusal text.
   - Other work releases the gate and resolves once more. If another login is
     usable, the launch takes that login's gate, overwrites `login_id` and
     records the switch. If none is usable, it fails the same typed way and parks
     under the first source in the setting.

**Probes with no task.** Project readiness resolves for the viewing member. The
native skill inventory is per `(provider, host)` and is not paid work. It runs
under any healthy pool login, the machine's own first. Added Codex homes link the
same `skills`, so the answer does not depend on the login.

## 3. Sessions, switches and invariant 10g

[AGENTS.md invariant 10g](../AGENTS.md#stable-invariants) forbids an episode from
silently switching to a fresh session. This design never starts a fresh session
on a login change. Each provider is handled as follows:

- **Claude: may switch at a turn boundary.** The token is only an environment
  variable. The session, config and skills stay in the OS home, so the same
  native session resumes under any token. At each turn boundary the resolver runs
  again by the member's current setting. The session moves only when one of
  these holds:
  - its login is unhealthy;
  - the current setting no longer allows that login's source;
  - the setting prefers a source that now has a healthy login.

  Within the pool it stays put. Every move writes the `provider_login_switched`
  receipt, which the task inspector shows. That makes the switch explicit,
  not silent.
- **Codex: must wait.** A Codex session lives in its login's `CODEX_HOME`.
  Moving it would need a fresh session, which 10g forbids. A continued Codex
  session parks until its own login is healthy again. A fresh Codex session, for
  example a new Auto-research child, resolves by the setting like anything new.
- **OpenCode:** one login per host, so nothing switches.
- **Mid-turn:** nothing switches. A turn runs to completion on the login it
  started with.

There is no per-run, per-episode or per-agent choice, and no "use another login"
action. One episode may use several pool logins across its sessions. That is the
pool working as intended, and usage shows each one.

**Fences.** Same rules, per login row:

- Sign-out, a classified auth failure and restore each record a new generation.
- Only the generation captured at launch can fence. A late failure cannot fence a
  login that was verified again.
- Fencing one login never fences another, in the pool or outside it.
- The generation stays in memory on `AgentTaskExecution`, as today. It now sits
  beside the resolved `LoginKey`.

**Release.** `parked_provider_work` and `resume_provider_account` key by login.
A Codex watcher group finds its login through its origin task. Other watcher
groups are selected by the member's setting, like other unbound work. Every
category released today is still released: blocked recoveries, queued tasks,
failed Experiment-loop turns, watcher groups, and, through episode
reconciliation, reports and mail.

Three events release work:

- **Verifying login L** releases work bound to L, plus other parked work whose
  member's setting includes L's source.
- **Adding a pool login** that verifies counts as verifying it.
- **Changing a setting** releases that member's parked work that is not bound, for
  each source the new setting makes usable.

**Removing a pool login.** Any member may remove a pool login. The machine's own
login cannot be removed. Removal never cuts off running Codex work:

1. Remove takes the login's credential gate (`hold_blocking`), cancels any pending
   sign-in on it, and sets `draining_since`. From then on no new session is
   assigned to it, and Claude sessions move at their next turn.
2. While a running task or a non-terminal episode is bound to it, the login stays
   in use for that bound Codex work. It can still be verified if it fails.
   The confirm dialog for Remove shows how many are bound.
3. When nothing bound is left, a `reconcile_recovery` pass finishes the removal:
   - it records `signed_out` (new generation) and sets `removed_at`;
   - it deletes only the credential, the Claude token or the Codex `auth.json`.
     Codex sessions and state stay, so ingestion still reads them;
   - deletion on a remote host is best effort. A host it cannot reach is retried
     on later passes and shows on the login's row until the deletion succeeds.
4. A Codex chat bound to a removed login fails its next turn with
   `session_refused` ("start a new chat").

**Credential gate.** `ProviderCredentialGate` keys by `(provider, host,
login_id)`, so Codex startups on different logins no longer wait for each other.
Only the machine's own login keeps the cross-process lock file. Added credentials
sit inside one data directory, so no other RCP process shares them.

**Member removal.** `ServerControlMembers.advance` already pauses the member's
tasks and fences their episodes. Next to `remove_member_data()`, it:

1. records `signed_out` (new generation) on the member's own logins, sets
   `removed_at`, and deletes their setting rows;
2. deletes the credentials of those logins locally. Codex sessions and state stay;
3. deletes the same credentials on each remote host. A host it cannot reach is
   retried on each later `advance` pass while the removal is pending, and never
   blocks `complete_member_removal`. Retries stop at completion. Every pass,
   including the completing one, lists the hosts still unreached in a new
   `provider_cleanup_unreached` field of the advance result. Those credentials are
   already fenced, so RCP never uses them.

Pool logins the member added stay in the pool, because they belong to the
machine. Restore applies the same steps to each member that `server_ops/restore.py`
removes.

## 4. Provider homes

| Provider | Machine's own login | Added login, local | Added login, remote |
| --- | --- | --- | --- |
| Claude | Token in `<account dir>/setup-token`; remote `~/.config/rcp/claude-setup-token` | Token in `<account dir>/logins/<login_id>/setup-token` | `~/.config/rcp/logins/<login_id>/claude-setup-token` |
| Codex | Inherited `CODEX_HOME`, else `~/.codex` | `CODEX_HOME=<account dir>/logins/<login_id>/codex-home` | `CODEX_HOME=~/.rcp/provider-homes/codex/<login_id>` |
| OpenCode | The account's own CLI login | none | none |

"Added login" covers both pool and member logins. They differ only in `member_id`.

`ProviderAuthentication` hooks take `login_id` next to `host`:
`credential_available`, `credential_metadata`, `process_environment`, `save_token`,
`prepare_verification`, `verified` and `sign_out`.

**Claude.** Only the token changes, through `CLAUDE_CODE_OAUTH_TOKEN`. Its
config, sessions and skills stay in the OS home. `remote_token_prefix(path)`
already takes a path; token placement and sign-out take it too.

**Codex.** An added home holds its own `auth.json`, sessions and state. It links
`config.toml`, `rules`, `skills` and `AGENTS.md` back to the machine Codex home,
because the default app-server runtime reads user config and rules. Sessions are
not linked, so a Codex session stays in its home.

- `credential_available` for an added Codex login is its row being `signed_in`.
  RCP did that sign-in itself.
- `process_environment` exports `CODEX_HOME`. Device sign-in, verify and
  `codex logout` use the same value.
- A shipped helper module creates the home (mode 0700) and refreshes the links,
  locally and over SSH, like other remote-executed code. The links are not
  written into the `remote_prefix` string.
- If a link has turned into a regular file, because Codex rewrote it, the helper
  reports that instead of overwriting.
- The session indexer and Retry handoff read added session folders, built from
  the data directory and the login rows. Agents' `source_roots` do not list them,
  because hidden read hides those homes.

Before implementation, check on a real host that Codex reads all four links and
that a Codex config write keeps `config.toml` a link.

**OpenCode.** RCP does not sign OpenCode in. Its login lives under
`XDG_DATA_HOME`, together with its sessions. Pool or member logins for OpenCode
would first need RCP-managed sign-in. Until then every member uses the machine's
own login, whatever their setting, and the UI shows no Add and no setting for it.

**Hidden read.** `hidden_read_defaults` already hides local `providers/`. Its
remote file list adds `~/.config/rcp/logins` and `~/.rcp/provider-homes`. This
stays cooperative, not a security boundary (section 6).

**Terminals.** Unchanged. Mirrored terminals mount a tmpfs home and see no
provider login.

## 5. Settings UI and API

**Where the setting lives: the machine card.** The pool belongs to a machine.
Every agent runs on its project's base machine, so a member's choice is about that
machine, not about a project. Projects that share a base machine would only repeat
the same choice. Settings already gathers each machine's provider paths, limits
and compute on one space card. The standalone "Provider logins" section moves
into that card as a "Sign-ins" fold. Notices open that fold.

**Who sees and edits what.**

- Everyone sees every login's state, who added it, and when it last changed.
- `detail` and `source` are shown only on pool rows and on the viewer's own
  login, because provider evidence can contain an account email.
- Token details and device codes are never shown to anyone but the person signing
  in.
- Any member may add, sign in, verify, sign out or remove pool logins. Only the
  owner may act on a member login or change their own setting.

These are human HTTP routes. No agent command verb reaches them
([invariants 3 and 4](../AGENTS.md#stable-invariants)).

**API.**

- `GET /api/providers/logins` keeps its list shape. Each row gains `login_id`,
  `kind: "pool" | "member"`, `owner_user_id`, `owner_display_name`,
  `added_by_display_name` and `added_at`. The row is built field by field, not by
  spreading the storage record.
- `POST /api/providers/{provider}/logins/add` with `{host, kind}` creates a pool
  login (`kind: "pool"`) or the caller's own (`kind: "member"`).
  - The new row starts `signed_out`, and the response returns its `login_id`.
  - Where the provider supports device sign-in (Codex), it also starts one.
    Otherwise (Claude) the client follows with `…/token`.
  - An add that is never finished stays as a `signed_out` pool row that anyone
    may remove.
  - `kind: "member"` refuses when the caller already has a login on that host.
- The `sign-in`, `token`, `verify` and `sign-out` bodies gain `login_id`.
  New `POST …/logins/remove` with `{host, login_id}` removes a pool login
  (section 3).
- The device sign-in attempt id, today called `login_id`, is renamed
  `attempt_id`, so that `login_id` always names a login. The routes become
  `…/sign-in/{attempt_id}` and `…/sign-in/{attempt_id}/cancel`.
- The status object gains `login_id`. The runner's running sign-ins, recovered
  generations and resume counts key by `login_id`.
- A second start on a login that already has a pending sign-in returns 409 and
  names who started it. This replaces the spec rule "Concurrent device sign-ins
  join one operation". Status and cancel return 404 unless the caller started
  the attempt.
- List rows include the running sign-in's `user_code` and `verification_url`
  only for the member who started it. Everyone else sees only who is signing in.
- New `GET /api/providers/logins/settings` returns the caller's settings. New
  `PUT /api/providers/{provider}/logins/setting` with
  `{"host": "", "setting": "mine_first"}`.

`ProjectService.provider_logins_for` and `agent_login_accounts` build the same
rows for notices and follow the same mapping.

**The "Sign-ins" fold, per provider** (390px layout in brackets):

- **Header:** the provider name and the viewer's setting as a compact select
  ("Mine", "Pool", "Mine first", "Pool first"). [The select wraps under the name.]
- **Pool:** one row per pool login, with a state dot, who added it, and its
  actions (sign in, verify, sign out, remove). The machine's own login has no
  remove action. An icon-only Add button sits at the end of the list.
  [Rows stack. Actions stay icon-only.]
- **Yours:** the viewer's own login with its actions, or an icon-only Add when it
  does not exist.
- **Members:** one collapsed row with a healthy count. It expands to read-only
  rows of other members' own logins.
- **OpenCode:** only the machine's own login. No Add, no select.
- **Personal space:** only the pool. No "Yours", no Members row, no select.
- No captions. Labels, dots and icons carry the meaning.

**Notices.** `ProviderLoginNotice` and readiness resolve for the viewing member
for new launches, and use the bound login for parked Codex work. The notice is one
row: a state mark and one icon action that opens the machine card's "Sign-ins"
fold. There, a new team member can add their own sign-in or change the setting.
When the parked work waits on another member's own login, the row names them and
has no action.

## 6. What this does not give

- Every member's agents run as the same OS account. Any agent can read any token
  in `providers/` and any added home, because Claude and OpenCode shells are not
  bounded and hidden read is cooperative. This matches the lab stance that RCP
  puts no walls between members.
- Git transport, SSH and machine access stay shared, as today.
- RCP assumes pool logins can run the same models. A login whose plan lacks the
  requested model fails that turn as today. RCP does not infer plan limits from
  provider prose, so it cannot route around them.
- What members do get:
  - more than one account per machine, so a team can spread quota and rate
    limits;
  - their own account when they want it, with its own quota, model access and
    bill;
  - a vendor-side record of which account ran what.

**Usage and cost.** RCP adds no billing split of its own: each vendor bills each
account. The usage view can show which login paid by joining usage rows to the
task's `login_id`, with no new usage column. `authorized_by` already shows who
asked for the work. Removed logins keep their rows, so history still names them.

## 7. Verification

- An old-writer regression from a 0.4.35 database holding machine rows, parked
  work of every category, a running episode and a Codex chat. After the upgrade:
  - each row is that machine's own login;
  - current members have `mine_first`;
  - the Codex chat resumes on the machine's own login;
  - verifying the machine's own login releases everything.
- The migration on a copy of a real team database, never the live one.
- Pool selection:
  - two healthy pool logins take new sessions in least-recently-used order;
  - a continued Claude session stays on its pool login while it is healthy.
- Claude switch: a turn after its pool login is revoked runs on another allowed
  login, resumes the same native session, and writes `provider_login_switched`.
- Codex wait: a continued Codex session whose login is revoked parks, and
  verifying that login releases it. A fresh Codex session in the same episode
  takes another healthy login.
- Gate recheck: unbound work falls back to another login. Bound work fails as
  `provider_auth` through the typed flag.
- Setting change: a member who changes "Pool" to "Mine first" and has a healthy
  own login moves their Claude chat at the next turn, with reason
  `preferred_source_available`. Their parked unbound work is released.
- Fence isolation: a failure on one login fences no other login.
- Removing a pool login with a running bound Codex episode drains it: no new
  assignments, the episode keeps running, and the removal finishes after the
  episode ends. The row and Codex sessions stay. A later turn in a bound Codex
  chat fails with "start a new chat". Claude work moves at its next turn. A late
  verify of a removed login is refused.
- Concurrency:
  - two simultaneous new sessions with two healthy pool logins land on
    different logins;
  - a second sign-in start on one login gets 409.
- Previous login: a launch that failed before the provider started is not the
  previous login and writes no switch receipt.
- Lineage: a transport reattempt and a `graph_repair` correction keep their
  parent's login; a Codex fork uses its source session's login.
- Defaults: an existing member on a host that never had a login row, and on a
  machine added after the upgrade, still uses the pool.
- Privacy: nobody can read or cancel another person's device sign-in, see its
  code in the list, or see the `detail` of another member's own login.
- Member removal fences their own logins and keeps the pool logins they added. An
  unreachable host is listed and retried.
- A remote Codex launch on a reachable host with an added `CODEX_HOME`. It checks
  the four links, resume, a config write and session indexing.
- A served-app journey: add a pool login, set "Pool first", get a refused launch
  for a new member, open the notice into the fold. Plus the 390px check.

## Rejected alternatives

- **The first healthy login in a fixed pool order.** It piles all load onto one
  account, and it would need an ordering control.
- **Least-recently-used on every turn.** It spreads load more evenly, but each
  account switch drops the provider's prompt cache for the session, which raises
  cost.
- **A setting per project.** Projects on one base machine would repeat the same
  choice, and the pool already lives on the machine.
- **An episode-level pin.** Sessions are the real binding. Claude can switch
  safely at a turn boundary, and Codex is bound by its home. A pin would add a
  table and a race without protecting anything more.
- **Share Codex `sessions` across homes.** That would let Codex sessions switch
  too. But it depends on undocumented Codex storage, which may also hold thread
  state outside `sessions/`.
- **A separate OS account per member.** That would give real isolation, but the
  spec runs every team agent as the service account or the machine's configured
  SSH account.
