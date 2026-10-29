# Per-member provider sign-in

Status: design for review. No code yet. Before it ships, its rules move into
`docs/specs/providers-and-containment.md` and this file is deleted.

## Gist

Today the whole team shares one Claude or Codex sign-in per machine. With this
change each member can also use their own account, with its own quota, rate
limits and bill.

- Each member picks, per provider, which sign-ins their work may use:
  **mine only**, **mine first**, or **machine first**.
- Work runs for the member who asked for it. Background work already knows that
  member, so no new authority path is needed.
- An episode keeps the sign-in it started with. If that sign-in dies, the
  episode's work waits until the same sign-in is back. It never switches.
- Claude needs only a different token per member. Codex needs a separate home
  folder per member. OpenCode stays on the machine sign-in.

**What happens to your team**

| Situation | Result |
| --- | --- |
| Nobody adds their own sign-in | Same as today. Everyone uses the machine sign-in. |
| Everyone adds their own | Each member's work runs and bills on their own account. |
| Some do, some don't | Members with their own sign-in use it. The rest use the machine sign-in. |
| Someone joins after the upgrade | Their work is refused until they sign in or pick "machine first". |
| Someone signs the machine sign-in out | New work stops for everyone without their own signed-in account. |

Defaults: a personal space uses **mine first**. A team space uses **mine only**,
so using the shared sign-in is each member's opt-in. Members who exist at upgrade
get **mine first**, so nothing changes for them.

The spec says RCP creates no extra provider homes. This changes that: each
member's Codex sign-in gets its own folder. RCP still never moves running work
from one account to another.

## Terms

| Term | Meaning |
| --- | --- |
| Host | The machine the agent runs on. Empty means the RCP machine itself. |
| Login | One provider sign-in on one host, keyed `(provider, host, owner)`. |
| Machine login | `owner = ""`. Today's only kind. Any member may sign it in or out. UI label: "Machine". |
| Member login | `owner = <user_id>`. Only that member may sign it in, verify it, or sign it out. UI label: "Yours". |
| Order | Per `(member, provider)`: `member_only`, `member_first` or `machine_first`. |
| Pin | The login an episode is bound to for one `(provider, host)`. |
| Park | Work waits, without failing, until its login is signed in again. |
| Fence, generation | As in the providers spec, [Login state and failure](../docs/specs/providers-and-containment.md#login-state-and-failure). |

`machine_first` with no member login is "machine only". A member who never wants
their own account used simply does not sign it in.

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
- `graph_runs` (task rows): add `login_owner TEXT`. `NULL` means "launched before
  this change" and reads as the machine login.

The migration also:

- writes `member_first` for every current team member (`identity_kind =
  'team_member'`, `removal_started_at IS NULL`);
- pins every non-terminal episode to the machine login for each
  `(provider, host)` its task rows used;
- reinstalls the resource-version triggers on the rebuilt tables and registers
  the two new ones (`src/rcp/storage/resource_versions.py` requires this).

`ProviderLoginStateRecord` gains `owner: str = ""`, placed so the positional
insert in `_write_provider_login_state` lists it in the same order. A missing
machine row still reads as `signed_in`, generation 0. A missing member row reads
as `signed_out`.

**Credentials on disk.** `ProviderCredentialStore.account_dir(provider, host)`
gains `owner`. The machine login keeps its path, so nothing moves. A member login
lives under `<account dir>/members/<user_id>/`. User ids are canonical UUID4s, so
they are path-safe.

**Compatibility with old writers** (AGENTS.md, replay and compatibility):

- Canonical graph data does not change. Patches, replay and merges are untouched,
  and agent Patch input has no login field.
- Old login rows are machine logins. Task rows without `login_owner` use the
  machine login. Old queued and recovery rows without `authorized_by` use the
  machine login. Legacy watcher wakes are already terminalized and need nothing.
- Old parked work is released by verifying the machine login, as today.
- Restore: `detach_provider_logins_for_restore` fences every `signed_in` row,
  including member rows, and must carry `owner` into the rows it rebuilds.
  `providers/` stays out of backups, so member tokens and local member homes never
  enter an archive.
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

A member who steers someone else's episode spends that episode's pinned login,
not their own.

**Which login.** One resolver in `agents/provider_accounts.py`:

```
resolve_login(store, credentials, *, provider, host, member_id,
              episode_id, session_login) -> LoginKey | LoginRefusal
```

Rules, first match wins:

1. **Episode pin.** If `episode_id` has a pin for `(provider, host)`, use it.
2. **Bound session.** If the provider keeps sessions inside the login's home
   (Codex), and the task continues a session, use `session_login`. That is the
   `login_owner` of the task that started the session. Forks and watcher wakes
   inherit it the same way. Claude sessions live in the shared OS home, so Claude
   skips this rule and each turn uses its sender's login. Each profile declares
   which kind it is.
3. **Order.** Walk the member's order. Take the first login that is usable: its
   credential is present and its row is `signed_in`.
4. **Refuse.** The refusal names the first login in the order.

OpenCode always resolves to the machine login (section 4).

When an episode resolves through rule 2 or 3, the result becomes its pin. So an
episode that continues a chat's Codex session is pinned to that session's login.

`account_login_refusal(provider, host)` becomes a check on one already-resolved
`LoginKey`. Every caller goes through the resolver first.

**When.**

1. Admission (before task creation and budget debit) resolves without writing.
   With no usable login it refuses with `ProviderSignedOut`, as today.
   `admit_provider_task` gains the authorizer. `_start` swaps in the episode's
   `authorized_by` before admission instead of after.
2. The launch resolves again. In one transaction it inserts the episode pin
   (`INSERT OR IGNORE`), reads back the winner, and writes `login_owner` to the
   task row. A pin left by a launch that then fails is harmless.
3. The launch takes the credential gate for that login and checks it again.
4. If the login went bad while the launch waited, the launch releases the gate.
   The task goes back to queued and parks under that login. It does not pick
   another login.

**Probes with no task.** Project readiness resolves for the viewing member. The
native skill inventory is per `(provider, host)` and is not paid work. It runs
under any usable login on that host, machine first. Member Codex homes link the
same `skills`, so the answer does not depend on the login.

## 3. Pins, fences and revocation

**Pins.** An episode pins one login per `(provider, host)` (rules 1 and 2 above).
AGENTS.md invariant 10g forbids an episode from silently switching. The pin is how
this design honors that.

**Changing the order mid-episode** does not touch running episodes. It applies to
new episodes, and to new chat turns where rule 2 does not bind them.

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
A watcher group finds its login through its origin task. Every category released
today is still released: blocked recoveries, queued tasks, failed Experiment-loop
turns, watcher groups, and, through episode reconciliation, reports and mail.

Two events release work:

- verifying login L releases work pinned or bound to L, plus unbound queued work
  whose member's order includes L;
- `PUT …/logins/order` releases unbound queued work for each login the new order
  makes usable.

**Credential gate.** `ProviderCredentialGate` keys by `(provider, host, owner)`,
so members' Codex startups no longer wait for each other. Only the machine login
keeps the cross-process lock file. Member credentials sit inside one data
directory, so no other RCP process shares them.

**Member removal.** `ServerControlMembers.advance` already pauses the member's
tasks and fences their episodes. Next to `remove_member_data()`, it:

1. records `signed_out` (new generation) on each member login, and deletes their
   order rows;
2. deletes the local member directories;
3. runs `codex logout` and removes the Claude token file on each remote host, best
   effort. Hosts it cannot reach are listed in the removal result, never silently
   skipped.

The restore path in `server_ops/restore.py` does the same.

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
  written into the `remote_prefix` string.
- Native session discovery (`session_roots`, `codex_roots`) adds member homes,
  so Retry handoff and ingestion still find member sessions.

Before implementation, check on a real host that Codex follows the four links.

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

**Who sees and edits what.** Everyone sees every login's state, read-only. That
includes other members' logins, but never their token details or device codes.
Anyone may act on the machine login. Only the owner may act on a member login or
change their own order. These are human HTTP routes. No agent command verb reaches
them (invariants 3 and 4).

**API.**

- `GET /api/providers/logins` keeps its list shape. Each row gains
  `owner: "machine" | "member"`, `owner_user_id` and `owner_display_name`. The row
  is built field by field, not by spreading the storage record.
- The `sign-in`, `token`, `verify` and `sign-out` bodies gain
  `owner: "machine" | "member"`. `member` always means the caller.
- Device sign-in status gains `owner`. The runner's running sign-ins,
  `_recovered` and `_resume_counts` key by owner, so a member never joins another
  member's pending sign-in. `GET …/sign-in/{login_id}` and `…/cancel` return 404
  unless the sign-in is the machine's or the caller's.
- New `GET /api/providers/logins/orders` returns the caller's order per provider.
  New `PUT /api/providers/{provider}/logins/order` with
  `{"order": "member_first"}`.

`ProjectService.provider_logins_for` and `agent_login_accounts` build the same
rows for notices and follow the same mapping.

**Settings, Provider logins.** One card per provider and host, as today:

- a "Yours" row, a "Machine" row, and read-only rows for other members, each with
  its state dot;
- actions only on rows the viewer may act on;
- a compact select on the provider header for the order, labelled "Mine only",
  "Mine first", "Machine first";
- OpenCode shows only the Machine row and no select;
- in a personal space the select stays hidden until a member login exists;
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
  builds no fences between members.
- Git transport, SSH and machine access stay shared, as today.
- What members do get: separate vendor quotas, rate limits and billing, their own
  model access, and a vendor-side record of whose account ran what.

**Usage and cost.** RCP adds no billing split of its own: each vendor bills each
account. The usage view can show whose login paid by joining usage rows to the
task's `login_owner`, without a new usage column. `authorized_by` already shows
who asked for the work.

## 7. Verification

- An old-writer regression from a 0.4.35 database. It holds machine rows, parked
  work of every category, and a running episode. Upgrade it, then check: rows are
  machine logins, current members have `member_first`, the episode is pinned to
  machine, and verifying the machine login releases everything.
- The migration on a copy of a real team database, never the live one.
- A race test: two first launches in one episode end on one pin.
- Fence isolation: a member failure does not fence the machine row, and the
  reverse.
- Privacy: another member cannot read or cancel your device sign-in.
- Member removal fences the member's rows and parks their pinned work.
- A remote Codex launch on a reachable host with a member `CODEX_HOME`. It
  confirms the four links, resume and session discovery.
- A served-app journey through Settings and a refused launch, plus the 390px check.

## Open questions for the human

1. Should a parked episode offer an explicit "use another login" action? For
   Codex it would also start a fresh session. The design leaves it out.
2. Should the space owner be able to set the team's default order? For example,
   an all-shared lab would set "machine first" once instead of each new member
   doing it. The design leaves it out, because using the machine login is each
   member's own opt-in.

## Rejected alternatives

- **Resolve at every launch, even inside episodes.** That mixes accounts in one
  episode, which invariant 10g forbids, and breaks Codex resume.
- **Pin every chat session to its first login.** Another member's turn in a
  shared Claude chat would bill the first member, and chats would get stuck when
  that member signs out. Only Codex needs the binding, because its sessions live
  in the login's home.
- **Share Codex `sessions` across homes.** That would let Codex chats follow the
  sender too. But it depends on undocumented Codex storage, which may also hold
  thread state outside `sessions/`.
- **A separate OS account per member.** That would give real isolation, but the
  spec runs every team agent as the service account or the machine's configured
  SSH account.
