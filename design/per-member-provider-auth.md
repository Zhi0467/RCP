# Per-member provider sign-in

Status: design for review. No code yet. Before it ships, its rules move into
`docs/specs/providers-and-containment.md` and this file is deleted.

## Gist

- A provider login is keyed by `(provider, host, owner)`. The owner is a member
  id, or empty for the **machine login** that exists today.
- Each member picks a **login order** per provider: `member`, `member, machine`,
  `machine, member`, or `machine`. A launch takes the first usable login in the
  order of the member it runs for.
- Every task runs for its `authorized_by` member. Background work already
  inherits that member, so no new authority path is needed.
- The chosen login is **pinned** once per episode and once per native session.
  A pinned login never falls back. If it dies, the work parks until that same
  login is signed in again.
- Claude stays one environment variable per login. Codex gets one `CODEX_HOME`
  per member login. OpenCode stays machine-only, because RCP does not manage its
  sign-in.

This reverses one spec sentence: "No account switching or alternate provider
homes are created" (providers spec, Team execution accounts and credentials).
Accounts still never switch; member Codex homes are the new alternate homes.

Upgrading changes nothing. Old rows become machine logins. Members who exist at
upgrade get the order `member, machine`.

## Terms

| Term | Meaning |
| --- | --- |
| Login | One provider sign-in on one execution account (`host`, empty for local). |
| Machine login | `owner = ""`. Today's only kind. Any member may sign it in or out. |
| Member login | `owner = <user_id>`. Only that member may sign it in, verify it, or sign it out. |
| Login order | Per `(member, provider)`: which logins that member's work may use, in order. |
| Pin | The login a scope (episode or native session) is bound to after its first launch. |

## 1. Data model and migration

**Tables.** One storage schema migration (the next number after
`consolidation_targets_v1`):

- `provider_login_states`: add `owner TEXT NOT NULL DEFAULT ''`. The primary key
  becomes `(provider, host, owner)`. SQLite cannot change a primary key in place,
  so the migration copies into a new table. Every old row keeps `owner = ''`.
- `provider_readiness_snapshots`: add `owner` the same way. The key becomes
  `(provider, host, owner, binary)`.
- New `provider_login_orders(user_id, provider, order_json, changed_at)`.
  `order_json` is one of the four orders. No row means the default.
- New `episode_provider_logins(episode_id, provider, host, owner, pinned_at)`,
  primary key `(episode_id, provider, host)`. First writer wins.
- `graph_runs` (task rows): add `login_owner TEXT` and `login_generation INTEGER`,
  written once when the provider is certain to start. `NULL` means "launched before
  this change" and reads as the machine login.
- `AgentUsageRecord`: add `login_owner` (section 6).

`ProviderLoginStateRecord` gains `owner: str = ""`. A missing machine row still
reads as `signed_in`, generation 0, as today. A missing member row reads as
`signed_out`: nobody has signed it in.

**Default order.** No `provider_login_orders` row means:

- personal space: `member, machine`. A fresh desktop keeps working with the CLI
  login already on the Mac.
- team space: `member`. Using the machine login is opt-in.

The migration writes `member, machine` for every member of a team space that
exists at upgrade (from `space_users` where `removed_at IS NULL`). Their work keeps
resolving to the machine login until they add their own.

**Credentials on disk.** `ProviderCredentialStore.account_dir(provider, host)`
gains an `owner` argument. The machine login keeps its current path, so nothing
moves. A member login lives under `<account dir>/members/<user_id>/`. The user id
is a canonical UUID4, so it is path-safe.

**Old-writer and compatibility rules** (AGENTS.md, replay and compatibility):

- No canonical graph data changes. Patches, replay, and merges are untouched.
  Agent Patch input carries no login field.
- Old SQLite rows: login rows become machine logins. Task rows without
  `login_owner` resolve as machine. Usage rows without `login_owner` count as
  machine.
- Parked work from an old writer (`provider_auth` recoveries, queued tasks,
  watcher groups) is released by verifying the machine login, as today.
- Restore: `detach_provider_logins_for_restore` fences every `signed_in` row,
  member rows included. `providers/` stays backup-excluded, so member tokens and
  local member homes are never in an archive.
- Another RCP process on the same OS account (a second data directory) keeps
  using `~/.codex` and `~/.rcp/credential-locks/<provider>.lock`. The machine
  login keeps exactly those paths, so the two processes still serialize.
- Downgrade is not supported by reading the new table with old code
  (`extra="forbid"`). Rollback restores the pre-update database backup, which is
  the existing update rollback path.
- Required checks: an old-writer regression that starts from a 0.4.35 database
  with machine rows and parked work, upgrades, launches, and releases. Plus API
  and Web checks for the new fields.

## 2. Resolving a launch

**Which member.** The task's `authorized_by.user_id`. Every provider-launching
task already carries it:

| Work | Member it resolves for |
| --- | --- |
| Chat, Work, Discuss, Paper coach | The member who sent the turn. |
| Watcher wake | The origin task's member (`resolve_watcher_delivery_authorizer`). |
| Auto-research orchestrator and children | The episode's `authorized_by`; children must match it already. |
| Experiment loop turns, wrap-up reports | The episode's `authorized_by`. |
| Nightly consolidation | The schedule's `authorized_by`. |
| Recovery, Retry, transport reattempts | The original task's member and pin. |
| Legacy task with no `authorized_by` | Machine login only. |

**Which login.** One resolver, `resolve_login(task)`, in `agents/provider_accounts.py`:

1. If the task continues a native session (resume, wake, correction, fork,
   report), use the login recorded on the task that owns that session.
2. Else, if the task belongs to an episode, use the episode's pin for
   `(provider, host)` if one exists.
3. Else, walk the member's order. Take the first login whose credential is
   present and whose row is `signed_in`.
4. If nothing is usable, refuse. The refusal names the first login in the order
   (for example "Your Codex sign-in on gpu-1").

OpenCode always resolves to the machine login (section 4).

**When.** Admission (`background.py`, before task creation and budget debit)
runs the resolver read-only and refuses with `ProviderSignedOut` when nothing is
usable. It pins nothing. The launch resolves again, takes the credential gate
for the chosen login, and rechecks under the gate. If that login went bad while
the launch waited, and the scope has no pin yet, the launch drops the gate and
resolves once more. When the provider is certain to start (`before_start`), the
launch writes `login_owner` and `login_generation` to the task row. For an
episode it also inserts the episode pin. If another launch pinned first, the
launch uses that pin instead and rechecks.

Every caller of `account_login_refusal` today takes `(provider, host)`. Each
moves to a resolved login key: background admission, `episode_health`,
`machine_power`, the episodes API, episode reports, `provider_skills`, the
launcher, and the sign-in runner.

**Probes with no task.** Readiness for a project resolves for the viewing member.
The native skill inventory is per `(provider, host)` and lists skills, not paid
work. It runs under the machine login when usable, else the first usable member
login on that host, and records which one it used.

## 3. Pins, fences, and revocation

**Pins.** An episode pins one login per `(provider, host)` at its first launch.
Outside episodes, the pin is the native session: every continuation of a session
uses the login that session started with. Invariant 10g allows no silent
fallback inside a running episode, and a Codex native session lives in its home.
Pinning covers both.

**Changing the order mid-episode.** It does not touch running episodes or
existing sessions. It applies to the next new chat, new episode, or fresh session
outside an episode.

**A pinned login dies.** Existing parking applies, keyed by the login:

- provider evidence fences only the login the turn ran under, with the
  generation captured on the task row;
- queued work stays queued; recoveries stay blocked; watcher groups stay pending;
- the notice names the pinned login and whose it is.

There is no "switch to another login" action. The human signs that login in
again or stops the episode. See open question 1.

**Fences.** Unchanged rules, per login row:

- Sign-out, a classified auth failure, and restore record a new generation.
- Only the generation captured at launch can fence a login. A late failure
  cannot fence a login that was verified again.
- Signing out the machine login fences only the machine login. Members whose
  order includes `member` keep working on their own logins. Unpinned work of
  members who only allow `machine` is refused.

**Release.** `resume_provider_account` and `parked_provider_work` key by login.
Verifying login L releases:

- work pinned to L;
- queued, unpinned work whose member's order includes L. It resolves again at
  launch.

**Credential gate.** `ProviderCredentialGate` keys by `(provider, host, owner)`.
Members' Codex startups then run in parallel, because each has its own
`auth.json`. The local account lock file stays `<provider>.lock` for the machine
login and becomes `<provider>-<user_id>.lock` for a member login.

**Member removal.** `begin_member_removal` (storage/spaces.py) signs out each of
that member's logins through the ordinary sign-out path and deletes the order
rows. Their pinned work then parks and can only end by Stop. The member's work
was already losing its authorizer, so this adds no new stuck state.

## 4. Provider homes

| Provider | Machine login | Member login, local | Member login, remote |
| --- | --- | --- | --- |
| Claude | Token in `providers/claude/<account>/setup-token`; remote file `~/.config/rcp/claude-setup-token` | Token in `<account dir>/members/<user_id>/setup-token` | `~/.config/rcp/members/<user_id>/claude-setup-token` |
| Codex | The OS `~/.codex` | `CODEX_HOME=<account dir>/members/<user_id>/codex-home` | `CODEX_HOME=~/.rcp/provider-homes/codex/<user_id>` |
| OpenCode | The OS account's CLI login | none | none |

**Claude.** Only the token changes. `CLAUDE_CODE_OAUTH_TOKEN` already reaches
every process. Claude's config directory, sessions and skills stay in the OS home,
so resume works whatever the login.

**Codex.** A member home holds its own `auth.json`. RCP links three entries back
to the OS `~/.codex`: `sessions`, `skills` and `AGENTS.md`. Linked sessions keep
resume working, and linked skills keep the skill inventory the same for every
login. Codex exec turns already ignore user config, so `config.toml` is not
linked. The launch creates the home with mode 0700 and refreshes the links. A
remote launch does this in its `remote_prefix` and exports `CODEX_HOME`. Device
sign-in, verify and `codex logout` run with the same `CODEX_HOME`.

Before implementation, probe one thing on a real host: Codex follows a symlinked
`sessions` directory for `resume` and for writing, and two homes writing one
`sessions` directory do not collide. If it fails, drop the links. Then a Codex
session is bound to its home, which the pin already guarantees.

**OpenCode.** RCP does not sign OpenCode in (`manages_login` is false). Its
login lives under `XDG_DATA_HOME`, which also holds its sessions. Per-member
OpenCode needs RCP-managed sign-in first. Until then OpenCode uses the machine
login for every member, whatever the order.

**Hidden read.** `host_facts` already lists the default `auth.json` paths. It
must also list the member token directories and member Codex homes. This stays
cooperative, not a security boundary (section 7).

**Terminals.** Unchanged. Mirrored terminals mount a tmpfs home, so they see no
provider login.

## 5. Settings UI and API

**Who sees and edits what.**

- Every member sees and edits the machine login, as today.
- A member sees and edits only their own member logins and their own order.
- Nobody sees another member's member logins. A notice about another member's
  work names whose login is needed, so you know whom to ask.

**API.** Same routes, scoped by the caller:

- `GET /api/providers/logins` returns machine rows plus the caller's member rows.
  Each row gains `owner: "machine" | "member"`. The response gains the caller's
  order per provider.
- The `sign-in`, `token`, `verify` and `sign-out` bodies gain
  `owner: "machine" | "member"`. `member` always means the caller. No route takes
  another member's id.
- New `PUT /api/providers/{provider}/logins/order` with
  `{"order": ["member", "machine"]}`. It refuses any order that is not one of the
  four.

**Settings, Provider logins.** One card per provider and machine, as today. The
card gets two rows, "Yours" and "Machine". Each row keeps today's state dot and
actions. A segmented control on the provider header picks the order: "Yours",
"Yours → Machine", "Machine → Yours", "Machine". Its explanation is a hover title,
not a caption. OpenCode shows only the Machine row and no control. At 390px the
two rows stack and the control wraps under the provider name.

**Notices.** `ProviderLoginNotice` and readiness resolve as follows:

- for a new launch: the viewing member's order;
- for parked work: the pinned login.

The notice is one row with a state mark and one icon action that opens the
matching Settings row. When the login belongs to another member, there is no
action. The row names the member, and the hover title says that only they can
sign it in.

## 6. Usage and cost attribution

Usage rows copy `login_owner` from the task row. The usage snapshot then splits
by login: each member's own login, and the machine login. `authorized_by`
already says who asked for the work. `login_owner` says whose subscription paid.
There is no new page; the existing usage view gains the split.

## 7. What this does not give

- Every member's agents run as the same OS account. Any agent can read any
  token in `providers/` and any member home, because Claude and OpenCode shells
  are not bounded and hidden read is cooperative. This matches the lab stance
  that RCP builds no fences between members.
- Git transport, SSH and machine access stay shared, as today.
- What members do get: separate vendor quotas, rate limits and billing; their
  own model access; and a provider-side record of whose account ran what.

## Open questions for the human

1. Should a parked episode offer an explicit "use another login" action? It
   would re-pin and, for Codex without shared sessions, start a fresh session.
   The design leaves it out.
2. Should the space owner be able to turn the machine login off for some members?
   The design says no. Signing the machine login out already turns it off for
   everyone.

## Rejected alternatives

- **Resolve at every launch, not per scope.** This mixes accounts inside one
  episode and breaks Codex resume. It is the silent fallback 10g forbids.
- **One order per member for all providers.** The order is cheap to store per
  provider, and members often hold one personal subscription, not all of them.
- **A separate OS account per member.** That would give real isolation, but the
  spec runs every team agent as the service account or the machine's configured
  SSH account.
