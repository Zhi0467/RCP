# Per-member provider sign-in

Status: design for review. No code yet. Before it ships, its rules move into
`docs/specs/providers-and-containment.md` and this file is deleted.

## Gist

Today the whole team shares one Claude or Codex sign-in per machine. With this
change each member can also use their own account, with its own quota, rate
limits and bill.

- Each member picks, per provider, which sign-ins their work may use:
  **Mine only**, **Mine first**, or **Machine first**.
- Work runs for the member who asked for it. RCP already records who started each
  task, including background work.
- An episode keeps the sign-in it started with. If that sign-in dies, the
  episode's work waits until the same sign-in is back. It never switches.
- Claude needs only a different token per member. Codex needs a separate home
  folder per member. OpenCode stays on the machine sign-in.
- Everyone can see whether each member is signed in. Nobody sees anyone else's
  tokens or sign-in codes.

**What happens to your team**

| Situation | Result |
| --- | --- |
| Nobody adds their own sign-in | Same as today. Everyone uses the machine sign-in. |
| Everyone adds their own | Each member's work runs and bills on their own account. |
| Some do, some don't | Members with their own sign-in use it. The rest use the machine sign-in. |
| Someone joins after the upgrade | Their work is refused until they sign in or pick "Machine first". |
| You join a teammate's Codex chat or steer their episode | It runs on the sign-in it started with, so that account pays. In a Claude chat each sender pays. |
| The teammate whose sign-in a Codex chat or episode uses signs out | Everyone's turns there wait until that teammate signs in again. Nobody else can fix it. |
| That teammate is removed from the space | Their Codex chats and episodes stop; start a new chat. Claude chats carry on. |
| Someone signs the machine sign-in out | Episodes running on it pause. New work stops for everyone without a signed-in account of their own. |

Defaults: a personal space uses **Mine first**. A team space uses **Mine only**,
so using the shared sign-in is each member's own opt-in. Members who exist at
upgrade get **Mine first**, so nothing changes for them.

The spec says RCP creates no extra provider homes. This changes that: each
member's Codex sign-in gets its own folder. RCP still never moves running work
from one account to another.

## Terms

| Term | Meaning |
| --- | --- |
| Host | The machine the agent runs on. Empty means the RCP machine itself. |
| Login | One provider sign-in on one host, keyed `(provider, host, owner)`. |
| Machine login | `owner = ""`. Today's only kind. Any member may sign it in or out. UI row: "Machine". |
| Member login | `owner = <user_id>`. Only that member may sign it in, verify it, or sign it out. UI row: "Yours". |
| Order | Per `(member, provider)`: `member_only`, `member_first` or `machine_first` (UI: "Mine only", "Mine first", "Machine first"). |
| Pin | The login an episode uses for one `(provider, host)`. |
| Bound | A Codex chat stays on the login its session started with, because the session lives in that login's home folder. |
| Park | Work waits, without failing, until its login is signed in again. |
| Fence, generation | As in the providers spec, [Login state and failure](../docs/specs/providers-and-containment.md#login-state-and-failure). |

There is no "machine only" setting. Pick "Machine first" and don't add your own
sign-in.

## 1. Data model and migration

One storage schema migration, numbered after `consolidation_targets_v1`:

- `provider_login_states`: add `owner TEXT NOT NULL DEFAULT ''`. The primary key
  becomes `(provider, host, owner)`. SQLite cannot change a key in place, so the
  migration copies rows into a new table. Every old row keeps `owner = ''`.
- `provider_readiness_snapshots`: a cache. Drop it and recreate it keyed
  `(provider, host, owner, binary)`.
- New `provider_login_orders(user_id, provider, login_order, changed_at)`.
  `login_order` is a TEXT enum of the three orders. No row means the default.
- New `episode_provider_logins(episode_id, provider, host, owner)`, primary key
  `(episode_id, provider, host)`. A table rather than a query over task rows,
  because two first launches in one episode can race. First insert wins.
- `graph_runs` (task rows): add `login_owner TEXT`. `NULL` means "not resolved
  yet". `''` means the machine login.

The migration also:

- sets `login_owner = ''` on every existing task row, so all work from before the
  upgrade reads as machine work, and `NULL` keeps one meaning;
- writes `member_first` for every current team member (`identity_kind =
  'team_member'`, `removal_started_at IS NULL`);
- reinstalls the resource-version triggers on the rebuilt tables and registers
  the two new tables (`storage/resource_versions.py`);
- reinstalls the readiness deadline trigger with `owner` in its owner key and
  deletes the old deadline rows (`storage/resource_deadlines.py`).

`ProviderLoginStateRecord` gains `owner: str = ""`. A missing machine row still
reads as `signed_in`, generation 0. A missing member row reads as `signed_out`.

**Credentials and homes on disk.** `ProviderCredentialStore.account_dir(provider,
host)` gains `owner`. The machine login keeps its path, so nothing moves. A
member login lives under `<account dir>/members/<user_id>/`. User ids are
canonical UUID4s, so they are path-safe. That folder is outside backups and
transfers, like all of `providers/`. Member Codex sessions inside it are
therefore not backed up either, the same as machine sessions in `~/.codex` today.

**Compatibility with old writers** (AGENTS.md, replay and compatibility):

- Canonical graph data does not change. Patches, replay and merges are untouched,
  and agent Patch input has no login field.
- Old login rows are machine logins. Old task rows carry `login_owner = ''`.
  Old queued and recovery rows without `authorized_by` use the machine login.
  Legacy watcher wakes are already terminalized and need nothing.
- Episodes running at upgrade stay on the machine login through resolver rule 1.
  The migration does not write pins for them, because queued rows have no host
  until a manifest is read.
- Old parked work is released by verifying the machine login, as today.
- Restore: `detach_provider_logins_for_restore` fences every `signed_in` row,
  including member rows, and carries `owner` into the rows it rebuilds.
- A second RCP process on the same OS account keeps using the machine Codex home
  and `~/.rcp/credential-locks/<provider>.lock`. The machine login keeps both, so
  the two processes still take turns.
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
`LoginKey(provider, host, owner)` or a refusal. Its inputs are the provider, the
host, the member, the episode if any, and the continued session's login if any.
Rules, first match wins:

1. **Episode pin.** If the episode has a pin for `(provider, host)`, use it. An
   episode with no pin but with a task row for that provider whose `login_owner`
   is `''` was running at upgrade, because every later launch pins. It uses the
   machine login.
2. **Bound session.** For Codex, the only provider whose member login has its own
   home, a task that continues a session uses that session's login. That is the
   `login_owner` of the earliest task row with the same provider and
   `native_session_id`. Forks, watcher wakes and Paper
   coach sessions inherit it the same way. Claude sessions live in the shared OS
   home, so Claude skips this rule and each turn uses its sender's login.
3. **Order.** Walk the member's order. Take the first login that is usable: its
   credential is present and its row is `signed_in`.
4. **Refuse.** The refusal names the first login in the order. If an episode's
   Codex pin and a continued session's login differ, the resolver refuses with
   `session_refused` and names both, rather than resume in the wrong home.

OpenCode always resolves to the machine login (section 4).

When an episode resolves through rule 2 or 3, the result becomes its pin. So an
episode that continues a chat's Codex session is pinned to that session's login.

`account_login_refusal` becomes a check on one resolved `LoginKey`. Every caller
goes through the resolver first. The launcher's `stream`, `_login_refusal`,
`process_environment` and readiness cache, the credential gate, and the sign-in
runner's `settle_interrupted_sign_ins` and `reset_logins_without_credentials` all
take the `LoginKey` instead of `(provider, host)`.

**When.**

1. Admission, before task creation and budget debit, resolves without writing.
   With no usable login it refuses with `ProviderSignedOut`, as today.
   `admit_provider_task` gains the authorizer, so `_start` substitutes the
   episode's `authorized_by` before admission instead of after.
2. The launch resolves again. In one transaction it reads the login row and,
   only if that row is `signed_in` (a missing machine row counts as signed in),
   inserts the episode pin (`INSERT OR IGNORE`).
   It reads back the winning pin and writes `login_owner` to the task row.
3. The launch takes the credential gate for that login and checks it again.
4. If the login went bad while the launch waited:
   - pinned or bound work fails as `provider_auth` and parks under that login.
     Today this recheck raises a plain `ValueError` with no failure kind, so it
     parks nothing. A typed flag on `AgentTaskExecution`, beside
     `launch_refused`, records the refusal and its `LoginKey`. `_failure_kind`
     maps the flag to `provider_auth`, never by matching the refusal text;
   - other work releases the gate and resolves once more by its order. If a
     different login is usable, the launch takes that login's gate and overwrites
     `login_owner`. If none is, it fails the same typed way and parks under the
     first login in its order.

Until the provider starts, `login_owner` on work that is neither pinned nor bound
is provisional. Each later launch attempt, such as a Retry, resolves it again.

**Probes with no task.** Project readiness resolves for the viewing member. The
native skill inventory is per `(provider, host)` and is not paid work. It runs
under any usable login on that host, machine first. Member Codex homes link the
same `skills`, so the answer does not depend on the login.

## 3. Pins, fences and revocation

**Pins.** An episode pins one login per `(provider, host)` (rules 1 and 2 above).
[AGENTS.md invariant 10g](../AGENTS.md#stable-invariants) forbids an episode from
silently switching. The pin is how this design keeps that rule.

**Changing the order** never touches pinned or bound work. It applies to every
launch that resolves through rule 3, including queued work that has not started.

**A pinned or bound login dies.** Today's parking applies, keyed by the login:

- provider evidence fences only the login the turn ran under, with the generation
  captured at launch;
- queued work stays queued, recoveries stay blocked, watcher groups stay pending;
- the notice names the login and whose it is.

There is no "switch to another login" action. The human signs that login in again
or stops the episode (open question 1).

**Fences.** Same rules, per login row:

- Sign-out, a classified auth failure and restore each record a new generation.
- Only the generation captured at launch can fence. A late failure cannot fence a
  login that was verified again.
- Fencing the machine login never fences a member login, and the reverse.
- The generation stays in memory on `AgentTaskExecution`, as today. It now sits
  beside the resolved `LoginKey`.

**Release.** `parked_provider_work` and `resume_provider_account` key by login.
A Codex watcher group finds its login through its origin task. Other watcher
groups are selected by the member's order, like other unbound work. Every category released
today is still released: blocked recoveries, queued tasks, failed Experiment-loop
turns, watcher groups, and, through episode reconciliation, reports and mail.

Two events release work:

- verifying login L releases work pinned or bound to L, plus other parked work
  whose member's order includes L;
- `PUT …/logins/order` releases the member's parked work that is neither pinned
  nor bound, for each login the new order makes usable.

**Credential gate.** `ProviderCredentialGate` keys by `(provider, host, owner)`,
so members' Codex startups no longer wait for each other. Only the machine login
keeps the cross-process lock file. Member credentials sit inside one data
directory, so no other RCP process shares them.

**Member removal.** `ServerControlMembers.advance` already pauses the member's
tasks and fences their episodes. Next to `remove_member_data()`, it:

1. records `signed_out` (new generation) on each of the member's logins and
   deletes their order rows;
2. deletes their local credentials: the Claude token and the Codex `auth.json`.
   Codex sessions and state stay, so ingestion and other members' history still
   read them;
3. deletes the same two credentials on each remote host. A host it cannot reach
   is retried on each later `advance` pass while the removal is pending. It never
   blocks `complete_member_removal`. Retries stop at completion. Every pass,
   including the completing one, lists the hosts still unreached in a new
   `provider_cleanup_unreached` field of the advance result. Those credentials
   are already fenced, so RCP never uses them.

Work bound or pinned to a removed member's login fails with `session_refused`
and the reason "start a new chat", not a sign-in notice nobody can act on.
Restore applies the same steps to each member that `server_ops/restore.py`
removes.

## 4. Provider homes

| Provider | Machine login | Member login, local | Member login, remote |
| --- | --- | --- | --- |
| Claude | Token in `<account dir>/setup-token`; remote `~/.config/rcp/claude-setup-token` | Token in `<account dir>/members/<user_id>/setup-token` | `~/.config/rcp/members/<user_id>/claude-setup-token` |
| Codex | Inherited `CODEX_HOME`, else `~/.codex` | `CODEX_HOME=<account dir>/members/<user_id>/codex-home` | `CODEX_HOME=~/.rcp/provider-homes/codex/<user_id>` |
| OpenCode | The account's own CLI login | none | none |

`ProviderAuthentication` hooks take `owner` next to `host`: `credential_available`,
`credential_metadata`, `process_environment`, `save_token`,
`prepare_verification`, `verified` and `sign_out`.

**Claude.** Only the token changes, through `CLAUDE_CODE_OAUTH_TOKEN`. Its
config, sessions and skills stay in the OS home. `remote_token_prefix(path)`
already takes a path; token placement and sign-out take it too.

**Codex.** A member home holds its own `auth.json`, sessions and state. It links
`config.toml`, `rules`, `skills` and `AGENTS.md` back to the machine Codex home,
because the default app-server runtime reads user config and rules. Sessions are
not linked, so a Codex session stays in its home (rule 2).

- `credential_available` for a member Codex login is its row being `signed_in`.
  RCP did that sign-in itself.
- `process_environment` exports `CODEX_HOME`. Device sign-in, verify and
  `codex logout` use the same value.
- A shipped helper module creates the home (mode 0700) and refreshes the links,
  locally and over SSH, like other remote-executed code. The links are not
  written into the `remote_prefix` string. If a link has turned into a regular
  file, because Codex rewrote it, the helper reports that instead of overwriting.
- The session indexer and Retry handoff read member session folders, built from
  the data directory and the login rows. Agents' `source_roots` do not list them,
  because hidden read hides member homes.

Before implementation, check on a real host that Codex reads all four links and
that a Codex config write keeps `config.toml` a link.

**OpenCode.** RCP does not sign OpenCode in. Its login lives under
`XDG_DATA_HOME`, together with its sessions. Per-member OpenCode would first need
RCP-managed sign-in. Until then every member uses the machine login, whatever
their order.

**Hidden read.** `hidden_read_defaults` already hides local `providers/`. Its
remote file list adds the member token directory and `~/.rcp/provider-homes`.
This stays cooperative, not a security boundary (section 6).

**Terminals.** Unchanged. Mirrored terminals mount a tmpfs home and see no
provider login.

## 5. Settings UI and API

**Who sees and edits what.**

- Everyone sees every login's state, owner and change time.
- Only the owner and the machine row show `detail` and `source`, since provider
  evidence can contain an account email. Token details and device codes are
  never shown to anyone else.
- Anyone may act on the machine login. Only the owner may act on a member login
  or change their own order.

These are human HTTP routes. No agent command verb reaches them
([invariants 3 and 4](../AGENTS.md#stable-invariants)).

**API.**

- `GET /api/providers/logins` keeps its list shape. Each row gains
  `owner: "machine" | "member"`, `owner_user_id` and `owner_display_name`. The row
  is built field by field, not by spreading the storage record.
- The `sign-in`, `token`, `verify` and `sign-out` bodies gain
  `owner: "machine" | "member"`. `member` always means the caller.
- Device sign-in status gains `owner`. The runner's running sign-ins, recovered
  generations and resume counts key by owner, so a member never joins another
  member's pending sign-in. `GET …/sign-in/{login_id}` and `…/cancel` return 404
  unless the sign-in is the machine's or the caller's.
- New `GET /api/providers/logins/orders` returns the caller's order per provider.
  New `PUT /api/providers/{provider}/logins/order` with
  `{"order": "member_first"}`.

`ProjectService.provider_logins_for` and `agent_login_accounts` build the same
rows for notices and follow the same mapping.

**Settings, Provider logins.** One card per provider and host, as today:

- a "Yours" row and a "Machine" row, each with its state dot and actions;
- one collapsed "Members" row with a signed-in count. It expands to read-only
  rows, so the card stays short on a phone;
- a compact select on the provider header for the order;
- OpenCode shows only the Machine row and no select;
- in a personal space the select and the Members row stay hidden until a member
  login exists;
- at 390px the rows stack and the select wraps under the provider name;
- no captions. Labels carry the meaning.

**Notices.** `ProviderLoginNotice` and readiness resolve for the viewing member
for new launches, and use the pinned or bound login for parked work. The notice is
one row: a state mark and one icon action. For the viewer's own refusal, the
action opens the provider header, where both the "Yours" row and the order select
are. A new team member can then either sign in or pick "Machine first". When the
login is another member's, the row names them and has no action.

## 6. What this does not give

- Every member's agents run as the same OS account. Any agent can read any token
  in `providers/` and any member home, because Claude and OpenCode shells are not
  bounded and hidden read is cooperative. This matches the lab stance that RCP
  puts no walls between members.
- Git transport, SSH and machine access stay shared, as today.
- What members do get: separate vendor quotas, rate limits and billing, their own
  model access, and a vendor-side record of whose account ran what.

**Usage and cost.** RCP adds no billing split of its own: each vendor bills each
account. The usage view can show whose login paid by joining usage rows to the
task's `login_owner` (`''` is the machine login), with no new usage column. `authorized_by` already shows who
asked for the work.

## 7. Verification

- An old-writer regression from a 0.4.35 database holding machine rows, parked
  work of every category, and a running episode. After the upgrade:
  - rows are machine logins;
  - current members have `member_first`;
  - the episode stays on the machine login;
  - verifying the machine login releases everything.
- An episode created after the upgrade, by a `member_first` member who has a
  signed-in member login, pins that member login.
- The migration on a copy of a real team database, never the live one.
- A race test: two first launches in one episode end on one pin.
- Fence isolation: a member failure does not fence the machine row, and the
  reverse.
- Gate recheck: unpinned work falls back to the next login; pinned work fails as
  `provider_auth` through the typed flag and is released by verifying that login.
- A Codex pin that differs from the continued session's login is refused.
- Privacy: another member cannot read or cancel your device sign-in, or see your
  login `detail`.
- `PUT …/order` releases parked work that is neither pinned nor bound.
- The personal-space default (`member_first`, select hidden until a member login
  exists).
- Member removal fences the member's rows and keeps their Codex sessions. Work
  bound to them fails with "start a new chat". An unreachable host is listed and
  retried.
- A remote Codex launch on a reachable host with a member `CODEX_HOME`. It checks
  the four links, resume, a config write, and session indexing.
- A served-app journey through Settings and a refused launch, plus the 390px check.

## Open questions for the human

1. Should a parked episode offer an explicit "use another login" action? For
   Codex it would also start a fresh session. The design leaves it out.
2. Should the space owner be able to set the team's default order? For example,
   an all-shared lab would set "Machine first" once instead of each new member
   doing it. The design leaves it out, because using the machine login is each
   member's own opt-in.

## Rejected alternatives

- **Resolve at every launch, even inside episodes.** That mixes accounts in one
  episode, which invariant 10g forbids, and breaks Codex resume.
- **Bind every chat session to its first login.** Another member's turn in a
  shared Claude chat would bill the first member, and chats would get stuck when
  that member signs out. Only Codex needs the binding, because its sessions live
  in the login's home.
- **Share Codex `sessions` across homes.** That would let Codex chats follow the
  sender too. But it depends on undocumented Codex storage, which may also hold
  thread state outside `sessions/`.
- **Hide other members' logins.** It would keep the card shorter, but the lab
  puts no walls between members. A collapsed row keeps the phone layout short.
- **A separate OS account per member.** That would give real isolation, but the
  spec runs every team agent as the service account or the machine's configured
  SSH account.
