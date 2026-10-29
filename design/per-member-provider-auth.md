# Per-member provider sign-in

Status: design for review. No code yet. Before it ships, its rules move into
`docs/specs/providers-and-containment.md` and this file is deleted.

## Gist

Today each machine has exactly one Claude sign-in and one Codex sign-in, shared
by the whole team. With this change:

- Each machine keeps a set of **shared sign-ins** per provider. Any member with
  access to the machine can add one or remove one.
- Each member can also add **their own** sign-in on a machine.
- Each member sets, per machine and provider, two groups: **Yours** and
  **Shared**. Each group has an on/off switch, and the member picks which group
  is tried first. At least one group stays on. It is a setting, not a per-run
  choice. RCP applies it to every chat, episode and background turn.
- Among the shared sign-ins, a session stays on the same one while it is healthy.
  A new session takes the one used longest ago, which spreads load across
  accounts.
- When a sign-in dies or hits a usage limit, a Claude session moves to another
  allowed sign-in at its next turn, and RCP records the move. A Codex session
  cannot move, because Codex keeps the session inside its sign-in's folder. It
  waits, and the existing **Switch provider** stays available where it is offered
  today.
- Everyone can see whether each sign-in is healthy. Nobody sees anyone else's
  tokens or sign-in codes.

**What happens to your team**

| Situation | Result |
| --- | --- |
| Nobody changes anything | Same as today. Each machine's shared set holds its one existing sign-in. |
| A member adds a second shared account | New sessions spread across both. Running sessions stay where they are. |
| Everyone uses their own | Each member turns Shared off. Each member's work bills their own account. |
| Some use their own, some the shared ones | Each member's setting decides. |
| Someone joins after the upgrade | Their work is refused until they add their own sign-in or turn Shared on. |
| You join a teammate's Codex chat | It stays on the sign-in it started with, so that account pays. In a Claude chat your own setting decides. |
| That sign-in's owner is removed from the space | Codex chats bound to their own sign-in stop; start a new chat. Shared sign-ins they added stay. |

Machines that run no agents never consult any of this. Nothing changes for them.

There is no team or owner default. Without a saved setting, a team member has
only Yours on, so using the shared sign-ins is each member's own opt-in. In a
personal space everything is shared, since one person owns every sign-in.
Members who exist at upgrade get Yours then Shared, so nothing changes for them.

The spec says RCP creates no extra provider homes. This changes that: each added
Codex sign-in gets its own folder. RCP still never moves a running Codex session
from one account to another.

## Terms

| Term | Meaning |
| --- | --- |
| Host | The machine the agent runs on. Empty means the RCP machine itself. |
| Login | One provider sign-in on one host, keyed `(provider, host, login_id)`. |
| Machine sign-in | `login_id = ""`. Today's only login: the OS account's own CLI login for Codex and OpenCode, and the stored token for Claude. It is always one of the shared logins. It cannot be removed, only signed out. |
| Shared login | A login with no `member_id`: the machine sign-in, plus every login a member added as shared. Any member may sign it in, verify it, sign it out, add it or remove it. UI group: "Shared on <machine>". |
| Own login | A login with `member_id` set. One per member, provider and host. Only that member may act on it. UI row: "Yours". |
| Source | One of the two groups: own (the member's login) or shared (the machine's shared logins). |
| Setting | Per `(member, provider, host)`: which sources are on and in what order. One of four values: `own`, `own_then_shared`, `shared_then_own`, `shared`. The UI shows two groups, each with a switch, and a swap button for the order. "Both off" cannot be stored. |
| Usable | The credential is present, the row is `signed_in`, and the login is not removed. Bound work (rules 1 and 2) needs only this. |
| Assignable | Usable and not draining. New choices (rules 3 and 4) need this. "Healthy" in the table means assignable. |
| Limited | A recent turn on this login ended with `session_limit` (section 1, "Limit mark"). A soft mark that lapses. RCP still uses the login when nothing else allowed is free. |
| Bound | A Codex session stays on the login it started with, because the session lives in that login's home folder. |
| Turn boundary | The start of a new agent task, including human Resume and Retry. Two exceptions inherit their parent's login: a transport reattempt (its parent carries a `transport_auto_retry` receipt), and the initial `graph_repair` correction (admitted with `claim_graph_repair_parent=True`). A human Resume or Retry of a repair task is a boundary. |
| Previous login | For a continued session: the `login_id` of the latest task row with the same provider, host and `native_session_id` that reached provider start, other than the current task. A fresh session has none. |
| Park | Work waits, without failing, until its login is usable again. |
| Fence, generation | As in the providers spec, [Login state and failure](../docs/specs/providers-and-containment.md#login-state-and-failure). |

`shared_then_own` lets the shared accounts pay first, with the member's own
account as the backup.

## Decision table: setting × runtime event

This is the table an implementer tests against. Events run down the side so that
each cell has room. "Next turn" means the next turn boundary. Every resolved login
is written to the task row (`login_id`). Every move between logins inside one
session writes a `provider_login_switched` receipt with `from`, `to` and a reason.
Hovering a turn's agent chip names its login, and a moved turn's chip carries a
small switch mark. No row ever starts a fresh session on its own (invariant 10g).

In the two "Switch provider" rows, the column means your setting for the target
provider on that machine, not your setting for the current provider.

A receipt is written only when the resolved login differs from the previous
login. When several reasons apply, the first in this order wins:

1. `unhealthy`: the previous login is not assignable;
2. `limited`: the previous login carries the limit mark;
3. `sender_setting`: the previous row's `authorized_by` differs from this task's;
4. `setting_changed`: the setting's `changed_at` is later than the previous row's
   provider start;
5. `preferred_source_available`: the setting now prefers a source that has an
   assignable login without the mark.

The limit signal is the existing one: `_failure_classified_as(record,
"session_limit")`. It reads the `provider_terminal_error` receipt where one is
written, and otherwise the error text through `classify_terminal_error`, so Work,
Discuss and Experiment turns count too. Today's recovery keeps the session:
Auto-research resumes it exactly, Experiment episodes record a diagnostic and
wait for Retry, and chats keep it for the next turn.

**After a limit, who triggers the next turn.** Settlement runs the resolver as
a dry run for the same session. If the dry run returns a different login that is
not limited, under rule 3's continued-session meaning, episode work is retried
once automatically through its owner's existing path:

- Auto-research recovery already resumes on its own;
- Experiment loops get the `background.retry` that release already uses for
  `provider_auth`.

The retry fires at most once per (session, login that hit the limit). The
receipt records which, so a restart cannot re-arm it. A chat moves on the
member's next message or Retry. With no such login, everything stays as today.

| Event | `own` | `own_then_shared` | `shared_then_own` | `shared` |
| --- | --- | --- | --- | --- |
| **New session** (chat, episode, child) | Your login. If it is unhealthy, admission refuses, and the notice opens your row. If it is limited, it still runs. | Your login if it is healthy and not limited. Otherwise the shared login used longest ago. If nothing is healthy, refused, naming your login. | The shared login used longest ago without the mark. Otherwise your login if it is healthy and not limited. If every allowed login is limited, the shared login used longest ago; if no shared login is healthy, your limited login. If nothing is healthy, refused, naming the shared set. | The shared login used longest ago, preferring one that is not limited. If none is healthy, refused, naming the shared set. |
| **Your login hits a limit mid-episode, Claude** | The turn ends `session_limit`, and your login is marked limited. Existing recovery, as today: Auto-research resumes on your login and may hit the limit again; an Experiment waits for Retry. Notice: the existing limit notice. | The turn ends `session_limit`. The episode's next turn comes from the one automatic retry above, and the same session moves to a shared login (`limited`). It moves back (`preferred_source_available`) only after the mark is cleared by evidence: a completed turn on your login, or a Verify. A lapse never moves a running session. | Your login was in use only because no shared login was free. The next turn, triggered the same way, goes to a shared login without the mark (`limited`). If there is none, existing recovery as today. | Not applicable. |
| **Your login hits a limit mid-episode, Codex** | The session is bound. It waits for existing recovery or Retry on your login. Switch provider is offered where it is today. | Same session: bound, waits as before. New Codex sessions, such as new Auto-research children, start on a shared login. | Bound, waits as before. New Codex sessions start on a shared login. | Not applicable. |
| **The shared login a session is on hits a limit** | Not applicable. | Claude: the next turn (the automatic retry for episodes, the next message for chats) goes back to your login if it is assignable and not limited, else to another shared login without the mark (`limited`). Codex: bound, waits; new sessions avoid the limited login. | Claude: the next turn goes to another shared login without the mark (`limited`), else to your login if it is assignable and not limited (`limited`). It moves back to shared (`preferred_source_available`) only after a shared login's mark is cleared by evidence. Codex: bound, waits; new sessions avoid the limited login. | Claude: the next turn, triggered the same way, goes to another shared login without the mark (`limited`). Codex: bound, waits; new sessions avoid the limited login. |
| **Every allowed login is limited** | Runs on your login. | A continued session stays on its previous login while it is assignable, with no receipt. A fresh session runs on your login. | A continued session stays on its previous login, shared or yours, while it is assignable, with no receipt. A fresh session takes the shared login used longest ago. | A continued session stays on its previous login if it is still assignable, with no receipt. A fresh session takes the one used longest ago. |
| **Resume or Retry of a paused or limited session** | Runs for whoever pressed it; episode tasks for the episode's authorizer. Your login; if unusable, parks under it. | Claude: resolved as a new turn, so it may move (receipt). Codex: the bound login; if unusable it parks; if limited it runs. | Claude: resolved as a new turn, so it may move (receipt). Codex: as left. | Claude: resolved as a new turn; sticky if its login is still assignable and not limited. Codex: as left. |
| **A teammate presses Retry on your chat** | Claude: resolves by the teammate's setting, since they asked. Receipt `sender_setting` if the login differs. Codex: the bound login. Episode tasks: always the episode's authorizer. | Same rule. | Same rule. | Same rule. |
| **Switch provider on a run** | Existing handoff: a fresh session on the new provider, with the handoff receipt it records today. The new provider resolves by your setting for it on that machine: your login there. | Fresh session. Your login for the new provider if it is healthy and not limited, else that provider's shared login. | Fresh session. That provider's shared login first, then yours. | Fresh session on that provider's shared login. |
| **Switch to a provider whose own login is limited** | Runs on your limited login, since nothing else is allowed. It may hit the limit again. | Falls back to that provider's shared login. | Not affected while that provider has an assignable shared login. Otherwise runs on your limited login. | Not affected. |
| **A login is signed out or revoked mid-run** | The running turn finishes or fails on its login. Provider evidence fences only that login. The work parks as `provider_auth` under your login, and the notice offers sign-in. | Unbound work: if another allowed login is assignable, release runs right after settlement records the park, and the next turn moves (`unhealthy`). Otherwise it parks. This includes a Codex task that failed before the provider named its session. Bound Codex: parks until that login is verified; Switch provider where offered. | Same as `own_then_shared`, with the sources swapped. | Same as `own_then_shared`, over the shared logins. |
| **A new turn in a chat whose earlier turns ran on another login** | Claude: your login, the sender's. If it differs from the previous login, a receipt with reason `sender_setting`. Codex: the session's login, whoever sends, so its owner pays. If that login is unhealthy, the turn waits; start a new chat or switch provider. | Claude: by your setting, staying on the previous login if it is the source your setting picks. Codex: as left. | Claude: by your setting, staying on the previous login if it is a shared login that is assignable and not limited. Codex: as left. | Claude: the previous shared login if it is healthy and not limited, else the one used longest ago. Codex: as left. |
| **The shared login a session is on is being removed** | Not applicable. | Claude: moves at its next turn (`unhealthy`). Codex: keeps running on it, because draining is still usable, until nothing running is bound (section 3). | Same as `own_then_shared`. | Same as `own_then_shared`. |

When every allowed login is limited, the limit may have lifted. If it has not,
the turn ends `session_limit` again and existing recovery owns the next try.
Nothing is refused, and no automatic retry fires.

Release rule from the table: when unbound work parks as `provider_auth` while
another login its setting allows is assignable, it is released at once. It does
not wait for a Verify.

## 1. Data model and migration

One storage schema migration, numbered after `consolidation_targets_v1`:

- `provider_login_states`: rebuilt with key `(provider, host, login_id)`.
  `login_id TEXT NOT NULL DEFAULT ''`. Old rows keep `login_id = ''` and become
  each machine sign-in. New columns:
  - `member_id TEXT` (`NULL` for shared logins), with a unique index on
    `(provider, host, member_id)` where it is set and `removed_at IS NULL`;
  - `added_by`, `added_at`;
  - `draining_since`, set when a member removes a shared login that still has
    bound work (section 3);
  - `removed_at`, which keeps a removed login's row for usage history.
- New `provider_login_assignments(provider, host, login_id, last_assigned_at,
  limited_at)`. It is the least-recently-used clock plus the limit mark. It is a
  separate table so that writing it on every launch does not fire the
  resource-version triggers that make Settings refetch.
- `provider_readiness_snapshots`: a cache. Drop it and recreate it keyed
  `(provider, host, login_id, binary)`.
- New `provider_login_settings(user_id, provider, host, setting, changed_at)`.
  `setting` is a TEXT enum of the four values, so "both off" cannot be stored. `host = '*'` is a member's
  fallback for any host without its own row. Only the migration writes `*` rows
  (below). Lookup order: the host's row, then the member's `*` row, then the
  built-in default (`own` in a team space, `shared` in a personal one).
- `graph_runs` (task rows): add `login_id TEXT`. `NULL` means "not resolved yet".
  `''` means the machine sign-in.

There is no episode-level table. The binding that matters is per session.

The migration also:

- sets `login_id = ''` on every existing task row, so all work from before the
  upgrade reads as machine work, and `NULL` keeps one meaning;
- writes a `*` row with `own_then_shared` for every current team member
  (`identity_kind = 'team_member'`, `removal_started_at IS NULL`) and every
  provider. This covers hosts that never had a login row and machines added
  later, so nothing changes for existing members anywhere;
- reinstalls the resource-version triggers on the rebuilt tables and registers
  the new ones (`storage/resource_versions.py`);
- reinstalls the readiness deadline trigger with `login_id` in its owner key and
  deletes the old deadline rows (`storage/resource_deadlines.py`).

`ProviderLoginStateRecord` gains the new fields. A missing row for a machine
sign-in still reads as `signed_in`, generation 0, exactly as today. Other logins
always have a row, because adding one writes it.

Readers of `provider_login_states` skip removed rows: the Settings list, release,
and the resolver. `mark_provider_login_verified` and the sign-in runner refuse a
removed login, so a late verify or token paste cannot revive it.

**Limit mark.** When settlement finds `_failure_classified_as(record,
"session_limit")`, it sets `limited_at` on that task's login. Like a fence, a
mark or clear applies only from a task whose provider start is later than the
current `limited_at`, so an overlapping turn cannot undo a fresh mark.

The mark clears in three ways:

- **Evidence:** a completed provider turn on that login that started after
  `limited_at`.
- **Evidence:** a successful Verify.
- **Lapse:** after `PROVIDER_LOGIN_LIMIT_HINT_SECONDS` in `limits.py`.

Without the lapse, a login that is always skipped would never get a turn to clear
its mark. A lapse only reorders choices for new sessions. A running session moves
back to that login only after an evidence clear, so an early lapse costs at most
one new session's turn. RCP does not infer when or how widely
a limit applies. The mark only orders the choice, and a limited login still runs
when nothing else allowed is free. That keeps the spec rule that RCP does not
infer limit scope from prose.

**Credentials and homes on disk.** `ProviderCredentialStore.account_dir` gains
`login_id`.

- The machine sign-in keeps today's paths, so nothing moves.
- Every added login, shared or own, lives under `<account dir>/logins/<login_id>/`.
  Login ids are UUID4s, so they are path-safe.
- All of `providers/` stays out of backups and transfers. Codex sessions inside
  added homes are therefore not backed up either, the same as machine sessions in
  `~/.codex` today.

**Compatibility with old writers** (AGENTS.md, replay and compatibility):

- Canonical graph data does not change. Patches, replay and merges are untouched,
  and agent Patch input has no login field.
- Old login rows become each machine sign-in. Old task rows carry
  `login_id = ''`. Old queued and recovery rows without `authorized_by` use the
  machine sign-in. Legacy watcher wakes are already terminalized.
- Codex sessions started before the upgrade are bound to the machine sign-in,
  which is still the OS home, so they resume as before.
- Old parked work is released by verifying the machine sign-in, as today.
- Restore: `detach_provider_logins_for_restore` fences every `signed_in` row,
  including added ones, and carries `login_id` into the rows it rebuilds.
- A second RCP process on the same OS account keeps using the machine Codex home
  and `~/.rcp/credential-locks/<provider>.lock`. The machine sign-in keeps both,
  so the two processes still take turns.
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
| Human Resume and Retry | The member who pressed it, except episode tasks, which use the episode's authorizer (`_start`). |
| Transport reattempts, automatic recovery | The original task's member. |

**Which login.** One resolver in `agents/provider_accounts.py` returns a
`LoginKey(provider, host, login_id)` or a refusal. Its inputs are the provider,
the host, the member, the continued session's login if any, and the session's
previous login if any. Rules, first match wins:

1. **Same lineage.** A transport reattempt (parent has a `transport_auto_retry`
   receipt) or the initial `graph_repair` correction (`claim_graph_repair_parent`)
   keeps its parent row's login. If that login is not usable, the task waits.
2. **Bound session (Codex).** A task that continues a Codex session uses that
   session's login. That is the `login_id` of the earliest task row with the
   same provider and `native_session_id` that reached provider start.
   - A fork has no session id until the provider names it, so it uses its fork
     source session's login.
   - Watcher wakes and Paper coach sessions inherit the login the same way.
   - If that login is not usable, the task waits. If it was removed, the task
     fails with `session_refused` and the reason "start a new chat".
3. **Source.** Walk the sources the setting turns on, in the setting's order
   (own and shared, or shared and own).
   - Take the first source with an assignable login that is not limited.
   - What "limited" means here depends on the session. For a fresh session, a
     lapsed mark counts as clear. For a continued session, any login other than
     its previous login whose mark lapsed without evidence still counts as
     limited. So a running session never moves on a lapse.
   - If every allowed login is limited, a continued session keeps its previous
     login while that is assignable, with no receipt, whichever source it is in.
     Otherwise, take the first source with any assignable login.
4. **Login within the source.**
   - Own: the member's login.
   - Shared: the session's previous login, if it is shared, assignable and not
     limited. Otherwise the assignable shared login with the oldest
     `last_assigned_at`, preferring ones that are not limited. Ties go to the
     machine sign-in, then the oldest `added_at`.
5. **Refuse.** The refusal names the first source in the setting.

OpenCode has no added logins. Its only login is the machine sign-in (section 4).

Rule 4 runs inside the `BEGIN IMMEDIATE` transaction of "When" step 2. That
transaction picks the login, writes the task's `login_id` and updates
`last_assigned_at`, so two simultaneous new sessions land on different logins.
A sticky reuse also updates the clock, so a login carrying a busy session does
not look idle. The first assignment of a machine sign-in inserts its rows.

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
   the task row. If that login differs from the session's previous login, it
   also writes a `provider_login_switched` receipt. Its reason is one of
   `unhealthy`, `limited`, `setting_changed`, `preferred_source_available` or
   `sender_setting`.
3. The launch takes the credential gate for that login and checks it again.
4. If the login went bad while the launch waited:
   - **Bound work** (rules 1 and 2) fails as `provider_auth` and parks under that
     login. Today this recheck raises a plain `ValueError` with no failure kind,
     so it parks nothing. A typed flag on `AgentTaskExecution`, beside
     `launch_refused`, records the refusal and its `LoginKey`. `_failure_kind`
     maps the flag to `provider_auth`, never by matching the refusal text.
   - **Other work** releases the gate and resolves once more. If another login
     is usable, the launch takes that login's gate, overwrites `login_id` and
     records the switch. If none is usable, it fails the same typed way and parks
     under the first source in the setting.

**Probes with no task.** Project readiness resolves for the viewing member. The
native skill inventory is per `(provider, host)` and is not paid work. It runs
under any healthy shared login, the machine sign-in first. Added Codex homes link
the same `skills`, so the answer does not depend on the login.

## 3. Sessions, switches and invariant 10g

[AGENTS.md invariant 10g](../AGENTS.md#stable-invariants) forbids an episode from
silently switching to a fresh session. This design never starts a fresh session
on a login change. Only the existing human Switch provider does that, with its
existing handoff receipt.

- **Claude: may switch at a turn boundary.** The token is only an environment
  variable. The session, config and skills stay in the OS home, so the same
  native session resumes under any token. At each turn boundary the resolver runs
  again by the current setting. The session moves only when:
  - its login is not assignable, or carries the mark;
  - the setting no longer allows that login's source;
  - the setting prefers a source that now has an assignable login without the
    mark, and that source's mark, if any, was cleared by evidence;
  - or a different member sent the turn and the resolver picks a different login
    for them.

  Within the shared set it stays put. Every move writes the receipt.
- **Codex: must wait.** A Codex session lives in its login's `CODEX_HOME`.
  Moving it would need a fresh session, which 10g forbids RCP to start on its
  own. A continued Codex session parks or retries on its own login. A fresh Codex
  session resolves by the setting like anything new.
- **OpenCode:** one login per host, so nothing switches.
- **Mid-turn:** nothing switches. A turn runs to completion on the login it
  started with.

There is no per-run, per-episode or per-agent login choice, and no "use another
login" action. One episode may use several shared logins across its sessions.
That is the shared set working as intended, and usage shows each one.

**Fences.** Same rules, per login row:

- Sign-out, a classified auth failure and restore each record a new generation.
- Only the generation captured at launch can fence. A late failure cannot fence a
  login that was verified again.
- Fencing one login never fences another.
- The generation stays in memory on `AgentTaskExecution`, as today. It now sits
  beside the resolved `LoginKey`.

**Release.** `parked_provider_work` and `resume_provider_account` key by login.
A Codex watcher group finds its login through its origin task. Other watcher
groups are selected by the member's setting, like other unbound work. Every
category released today is still released: blocked recoveries, queued tasks,
failed Experiment-loop turns, watcher groups, and, through episode
reconciliation, reports and mail.

Five events release work:

- **Verifying login L** releases work bound to L, plus other parked work whose
  member's setting includes L's source.
- **Adding a shared login** that verifies counts as verifying it.
- **Changing a setting** releases that member's parked work that is not bound,
  for each source the new setting makes usable.
- **Settling unbound work as `provider_auth`.** If the member's setting still
  has an assignable login, the same code path that settles the task calls the
  login-keyed `resume_provider_account` for that login. It runs after the park is
  recorded, not at the fence, because `_failure_kind` writes the fence before the
  recovery row exists. This covers Codex tasks with no `native_session_id` too.
- **Settling unbound episode work as `session_limit`.** One automatic retry
  through the owner's existing path, when the resolver's dry run for that
  session returns a different login that is not limited ("After a limit, who
  triggers the next turn").

**Removing a shared login.** Any member may remove a shared login except the
machine sign-in. Removal never cuts off running Codex work:

1. Remove first cancels any pending sign-in on the login through the existing
   `cancel_sign_in`, because a device sign-in holds the gate for its whole
   attempt. It then sets `draining_since`, and only then takes the gate
   (`hold_blocking`). From then on no new session is assigned to it, and Claude
   sessions move at their next turn.
2. A draining login is still usable, so bound Codex work keeps running on it.
   The drain counts only running tasks and running, unpaused episodes bound to
   it. A paused episode does not hold the drain open. If it resumes after the
   removal finishes, it fails with `session_refused`. The login can still be
   verified if it fails. The confirm dialog for Remove shows how many are bound.
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
Only the machine sign-in keeps the cross-process lock file. Added credentials sit
inside one data directory, so no other RCP process shares them.

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

Shared logins the member added stay, because they belong to the machine. Restore
applies the same steps to each member that `server_ops/restore.py` removes.

## 4. Provider homes

| Provider | Machine sign-in | Added login, local | Added login, remote |
| --- | --- | --- | --- |
| Claude | Token in `<account dir>/setup-token`; remote `~/.config/rcp/claude-setup-token` | Token in `<account dir>/logins/<login_id>/setup-token` | `~/.config/rcp/logins/<login_id>/claude-setup-token` |
| Codex | Inherited `CODEX_HOME`, else `~/.codex` | `CODEX_HOME=<account dir>/logins/<login_id>/codex-home` | `CODEX_HOME=~/.rcp/provider-homes/codex/<login_id>` |
| OpenCode | The account's own CLI login | none | none |

"Added login" covers both shared and own logins. They differ only in `member_id`.

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
`XDG_DATA_HOME`, together with its sessions. Shared or own logins for OpenCode
would first need RCP-managed sign-in. Until then every member uses the machine
sign-in, whatever their setting, and the UI shows no Add and no setting for it.

**Hidden read.** `hidden_read_defaults` already hides local `providers/`. Its
remote file list adds `~/.config/rcp/logins` and `~/.rcp/provider-homes`. This
stays cooperative, not a security boundary (section 6).

**Terminals.** Unchanged. Mirrored terminals mount a tmpfs home and see no
provider login.

## 5. Settings UI and API

**Where the setting lives: the machine card.** The shared logins belong to a
machine. Every agent runs on its project's base machine, so a member's choice is
about that machine, not about a project. Projects that share a base machine would
only repeat the same choice. Settings already gathers each machine's provider
paths, limits and compute on one space card. The standalone "Provider logins"
section moves into that card as a "Sign-ins" fold. Notices open that fold.

**Layout carries the meaning.** Each provider is one raised panel. Inside it:

- **Two sunken trays.** "Yours" holds your sign-in card. "Shared on <machine>"
  holds the machine sign-in card, any added cards, and an add slot.
- **A round swap button** between the trays.
- **A switch** in each tray's header.

Left to right is the order your work tries the trays. At 390px the trays stack
top to bottom, and the swap icon turns to point up and down. The Members row sits
collapsed below the trays. The words "Mine", "Pool" and "Runs on" do not appear.

Each switch and swap writes the setting at once (`PUT …/logins/setting`).

| Stored value | Trays, left to right | Yours switch | Shared switch | Swap |
| --- | --- | --- | --- | --- |
| `own_then_shared` | Yours, Shared | on | on | enabled; stores `shared_then_own` |
| `shared_then_own` | Shared, Yours | on | on | enabled; stores `own_then_shared` |
| `own` | Yours, Shared | on, locked | off | disabled |
| `shared` | Shared, Yours | off | on, locked | disabled |

- With one group on, the tray that is on is drawn first.
- Turning the other group back on stores "the tray that is on, then the other".
  For example, `shared` with Yours turned on stores `shared_then_own`.
- With one group on, the swap is disabled and writes nothing. Its hover reads
  "Turn both on to choose the order".
- The locked switch is the only group that is on. A stale client that tries to
  turn it off gets 422, because the body schema has no "both off" value.

**States.**

| Element | State | Looks like |
| --- | --- | --- |
| Tray | Switched off | Fades. Its cards flatten. |
| Card | Ready | Raised, reads "Ready". |
| Card | Signed out | Flat, dashed border, with a "Sign in" button. |
| Card | Limited | Raised, amber bottom edge, reads "Usage limit". |
| Card | Draining | Reads "Being removed". |
| Card | Signing in, seen by others | Reads "<name> is signing in". |
| Card | Signing in, seen by the starter (Codex) | Shows the device code (copyable), the verification link, and a Cancel icon. |
| Card | Entering a token (Claude) | Expands in place into a password field and a Save icon. Opened by ＋ or by "Sign in". |

When two states apply, the first in this order wins: Draining, Signing in,
Signed out, Limited, Ready.

Card actions sit behind a ⋯ that shows on hover. At 390px, where there is no
hover, the ⋯ is always shown.

At 390px, one provider panel:

```
┌ Claude ─────────────────────────┐
│ ┌ Yours ───────────────── ◉ ┐   │
│ │ [ Ready              ⋯ ]  │   │
│ └───────────────────────────┘   │
│              ( ⇅ )              │
│ ┌ Shared on gpu-1 ──────── ◉ ┐  │
│ │ [ Machine sign-in · Ready ⋯ ]│
│ │ [ Ana · Usage limit       ⋯ ]│
│ │ ┊ Bo           [Sign in]  ┊ │ │
│ │ ＋                          │ │
│ └─────────────────────────────┘ │
│ Members ▸ 3 of 5                │
└─────────────────────────────────┘
```

**Exact labels and hover texts.** Hover texts are `title` and tooltip text only.
No captions appear on the page.

| Element | Label | Hover |
| --- | --- | --- |
| Yours tray | "Yours" | "Your own <Provider> account on <machine>. Only you can change it." |
| Yours tray, no sign-in | add slot ＋ | "Add your own <Provider> sign-in" |
| Shared tray | "Shared on <machine>" | "Any member can use, add or remove these." |
| Tray switch, on | (switch) | "Used" |
| Tray switch, off | (switch) | "Not used" |
| Last switch that is on | (switch, locked) | "One group must stay on" |
| Swap button | ⇄ (⇅ at 390px) | "Swap the order" |
| Machine sign-in card | "Machine sign-in" | "This machine's own <Provider> login." |
| Added shared card | "<name>" | "Added by <name> on <date>." |
| Shared add slot | ＋ | "Add a shared <Provider> sign-in" |
| Ready | "Ready" | "Signed in · checked <time>" |
| Limited | "Usage limit" | "Hit a usage limit <time>. Used when nothing else is free." |
| Draining | "Being removed" | "<n> running" |
| Signed out | "Sign in" (button) | (none) |
| More | ⋯ | "More" |
| Menu items | "Check sign-in", "Sign out", "Remove from shared" | (none) |
| Cancel (device sign-in) | icon | "Cancel sign-in" |
| Save (token) | icon | "Save token" |
| Swap, one group on | ⇄ (disabled) | "Turn both on to choose the order" |
| Members row | "Members" plus "▸ n of m" | "Other members' own sign-ins. Signed in: n of m." m is the other current members of the space; n is how many of them have a signed-in own login on this machine. |

A refused launch's notice opens this panel. A new member then sees both fixes
side by side: the add slot in "Yours", or the switch on "Shared".

A personal space shows only the shared tray, titled with the machine name alone.
It has no Yours tray, no switches, no swap and no Members row. OpenCode shows only
its machine sign-in card, with no switch, no add slot and no ⋯ actions.

**Who sees and edits what.**

- Everyone sees every login's state, who added it, and when it last changed.
- `detail` and `source` are shown only on shared rows and on the viewer's own
  login, because provider evidence can contain an account email.
- Token details and device codes are never shown to anyone but the person signing
  in.
- Any member may add, sign in, verify, sign out or remove shared logins. Only the
  owner may act on an own login or change their own setting.

These are human HTTP routes. No agent command verb reaches them
([invariants 3 and 4](../AGENTS.md#stable-invariants)).

**API.**

- `GET /api/providers/logins` keeps its list shape. Each row gains `login_id`,
  `kind: "shared" | "own"`, `owner_user_id`, `owner_display_name`,
  `added_by_display_name`, `added_at`, `limited_at` and `draining_since`. The row
  is built field by field, not by spreading the storage record.
- `POST /api/providers/{provider}/logins/add` with `{host, kind}` creates a
  shared login (`kind: "shared"`) or the caller's own (`kind: "own"`).
  - The new row starts `signed_out`, and the response returns its `login_id`.
  - Where the provider supports device sign-in (Codex), it also starts one.
    Otherwise (Claude) the client follows with `…/token`.
  - An add that is never finished stays as a `signed_out` shared row that anyone
    may remove.
  - `kind: "own"` refuses when the caller already has a login on that host.
- The `sign-in`, `token`, `verify` and `sign-out` bodies gain `login_id`.
  New `POST …/logins/remove` with `{host, login_id}` removes a shared login
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
  `{"host": "", "setting": "shared_then_own"}`. The body takes exactly one of
  the four values.

`ProjectService.provider_logins_for` and `agent_login_accounts` build the same
rows for notices and follow the same mapping.

**Notices.** `ProviderLoginNotice` and readiness resolve for the viewing member
for new launches, and use the bound login for parked Codex work. The notice is one
row: a state mark and one icon action that opens the machine card's "Sign-ins"
fold. When the parked work waits on another member's own login, the row names
them and has no action.

## 6. What this does not give

- Every member's agents run as the same OS account. Any agent can read any token
  in `providers/` and any added home, because Claude and OpenCode shells are not
  bounded and hidden read is cooperative. This matches the lab stance that RCP
  puts no walls between members.
- Git transport, SSH and machine access stay shared, as today.
- RCP assumes shared logins can run the same models. A login whose plan lacks the
  requested model fails that turn as today. RCP does not infer plan limits from
  provider prose, so it cannot route around them.
- The limit mark is a hint, not a schedule. RCP does not know when a limit
  lifts. It never holds work back on a timer; it only prefers other logins until
  the mark clears or lapses.
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

**The decision table.** One test per non-"not applicable" cell, for both Claude
and Codex where the cell differs. Each test checks:

- the `login_id` written to the task;
- whether the session moved or waited;
- the receipt and its reason;
- the notice state.

**Migration and compatibility.**

- An old-writer regression from a 0.4.35 database holding machine rows, parked
  work of every category, a running episode and a Codex chat. After the upgrade:
  - each row is that machine's sign-in;
  - current members have `own_then_shared` through `*` rows;
  - the Codex chat resumes on the machine sign-in;
  - verifying the machine sign-in releases everything.
- The migration on a copy of a real team database, never the live one.
- An existing member on a host that never had a login row, and on a machine added
  after the upgrade, still uses the shared logins.

**Selection and concurrency.**

- Two healthy shared logins take new sessions in least-recently-used order.
- Two simultaneous new sessions land on different logins.
- A continued Claude session stays on its shared login while it is healthy and
  not limited.
- A limited login is skipped while another allowed one is free, and still runs
  when none is. Its mark clears after a completed turn or a Verify, and lapses
  after the hint time.
- A turn that started before the mark and completes after it leaves the mark in
  place.
- A lapse reorders new sessions but does not move a running session back.
- When every shared login is limited, a continued session stays put and writes no
  receipt.
- An Experiment on `own_then_shared` whose own login hits a limit is retried
  once automatically and moves to a shared login.
- `shared_then_own`:
  - a new session starts on a shared login;
  - when every shared login is limited or signed out, the next turn falls back
    to your own login (`limited` or `unhealthy`);
  - after a shared login's mark is cleared by evidence, the next turn returns to
    shared (`preferred_source_available`).
  - after a shared login's mark merely lapses, a running session stays on your
    login, and no automatic retry fires.
- The automatic retry fires at most once per (session, limited login), including
  across a restart.

**Lineage and sessions.**

- A transport reattempt and a `graph_repair` correction keep their parent's
  login.
- A Codex fork uses its source session's login.
- A launch that failed before the provider started is not the previous login and
  writes no switch receipt.

**Sign-in lifecycle.**

- Gate recheck: unbound work falls back to another login, and bound work fails as
  `provider_auth` through the typed flag.
- Release after settlement: unbound work parked as `provider_auth` is released at
  once when another allowed login is assignable.
- Retry pressed by a teammate resolves by the teammate's setting. In an episode,
  the episode's authorizer is used.
- A human Retry of a `graph_repair` task is a turn boundary. The initial
  correction keeps its parent's login.
- Fence isolation: a failure on one login fences no other login.
- Removing a shared login with a running bound Codex episode drains it:
  - the episode keeps launching turns on the draining login (no deadlock);
  - Remove during a pending device sign-in cancels it first;
  - no new assignments;
  - the episode keeps running, and removal finishes after it ends;
  - the row and Codex sessions stay;
  - a later turn in a bound Codex chat fails with "start a new chat";
  - a late verify of the removed login is refused.
- A second sign-in start on one login gets 409.
- Privacy: nobody can read or cancel another person's device sign-in, see its
  code in the list, or see the `detail` of another member's own login.
- Member removal fences their own logins and keeps the shared logins they added.
  An unreachable host is listed and retried.

**Real host and app.**

- A remote Codex launch on a reachable host with an added `CODEX_HOME`. It checks
  the four links, resume, a config write and session indexing.
- A served-app journey:
  1. add a shared login;
  2. set the order with the swap button and the switches, and check each of the
     four stored values. Include the sequence: switch Yours off, back on, then
     swap. Include a 422 for a stale request that turns the last group off;
  3. get a refused launch for a new member;
  4. open the notice into the fold;
  5. check every hover text.

  Plus the 390px check against the sketch: trays stack, the swap icon turns,
  and ⋯ stays visible.

## Rejected alternatives

- **Three fixed choices in one select.** It hid that the setting is an order,
  and it had no way to put shared first.
- **A ranked list of single logins.** It would lose load spreading inside the
  shared group.
- **One order across providers.** RCP would then switch provider on its own,
  which today only a member does.
- **The first healthy login in a fixed order.** It piles all load onto one
  account, and it would need an ordering control.
- **Least-recently-used on every turn.** It spreads load more evenly, but each
  account switch drops the provider's prompt cache for the session, which raises
  cost.
- **A cooldown that holds work back.** RCP cannot know when a vendor limit
  lifts. A soft mark that only orders the choice, and lapses, never strands work
  on a guess.
- **A setting per project.** Projects on one base machine would repeat the same
  choice, and the shared logins already live on the machine.
- **An episode-level pin.** Sessions are the real binding. Claude can switch
  safely at a turn boundary, and Codex is bound by its home.
- **Share Codex `sessions` across homes.** That would let Codex sessions switch
  too. But it depends on undocumented Codex storage, which may also hold thread
  state outside `sessions/`.
- **A separate OS account per member.** That would give real isolation, but the
  spec runs every team agent as the service account or the machine's configured
  SSH account.
