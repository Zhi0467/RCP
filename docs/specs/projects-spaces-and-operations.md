# Projects, spaces, and operations

Space/project identity, membership, setup, caches and operational lifecycle.
[Machine operations](server-and-machine-operations.md) owns deployment,
provisioning, transfer and backup.

## Spaces and durable identity

A space owns one SQLite control plane, project catalog, operational/execution
state and team credentials. It mints random `space_id` and immutable `personal`
or `team` kind (legacy installs migrate personal). Identity survives restart,
address/path changes, upgrade and authorized relocation; it is never inferred
from host, files, credentials or users. `/api/health` separately reports transient
`instance_id` and path-derived `data_dir_id`. A restored copy shares authority:
operators must prevent the old copy from serving.

Personal spaces have one durable owner and separate credential. Teams authenticate
every human request with equal member authority, without an administrator role.

### Personal sign-in

Personal APIs and terminal sockets require owner sessions. Static shell, adoption
health, credential exchange/code redemption and OPTIONS are public; phone pairing
has its own listener. Display-name selection grants no authentication.

Desktop Keychain stores a 32-byte secret keyed by data-directory identity. Only
fresh initialization under the instance lock enrolls its hash; exchange never
enrolls. Each personal start issues a single-use ten-minute code, locked after
repeated guesses. Human launch prints a URL; machine launch returns only outcome
JSON with `owner_sign_in_code`, absent for reused/team outcomes. Redemption may
replace the secret and revoke old sessions. Raw codes/secrets are never persisted.

Owner/team sessions share hashed storage, expiry and revocation, but separate
admission and cookies. Owner cookie is host-only HttpOnly SameSite=Strict, Secure
on HTTPS. Snapshots exclude owner credentials/codes/sessions; restore requires
fresh personal sign-in.

### Space machines

One machine card per unique host records everything configured on that machine:
name, account, writable paths, hidden folders, provider limits, provider
executables, job manager, jobs root and access hint. A project only adds or
removes cards under an alias its repositories and agent profiles name; it
configures none of them, and Project Settings shows the same card. A project
detaches a machine only while no repository lives on it; agent profiles that ran
on it move to the base machine. Cards are
space state and do not travel with a transferred project.

**Why:** settings kept per project disagreed for one machine, and a fix in one
project's Settings silently missed the others.

Older builds kept `provider_paths`, `compute` and `access_hint` on the
manifest's machine entries. A card that existed before this layout, or that a
manifest creates, imports them once: Slurm turns on when any project named it,
and every other value fills only an empty field. The import runs before
startup recovery relaunches work. Only a start that read every project settles
cards; registering one project merges without settling. A human edit to a card
settles it, so no later start refills a field the human cleared.
The keys stay inert in the manifest, so backups record the file as written.

SSH
selects account through the route; a second account on one host refuses. In a
team space, a blank account on Add machine is filled from a batch-mode login as
the service account; a personal card keeps it blank. An unknown host key shows its SHA256 fingerprint with **Trust**. SSH
itself fetches the key over the account's own route (proxy, port, algorithms,
host-key alias) into a scratch file, offering no credential. Trust rescans,
refuses if the key changed since it was shown or any user or global known-hosts
file already names the host, and appends it to the first user `known_hosts`. RCP never trusts a key on its own, and a
changed host key cannot be overridden from the UI. Every command a card asks a
human to run is rendered by `operator_commands` as typed from that human's own
shell: on a team server that is their own login, with `sudo -u <service> -H` and
the service account's SSH route to reach another machine. Project
manifests own each machine's alias, host, and account; cards own its settings.
Setup/Add machine copies card host/account
and rechecks account even when the route omits user. Space card creation never
writes manifests. Startup, registration and Add machine fill cards from accepted
manifests; conflicting accounts produce a nameless account and logged warning.
Raw loads, preflight, replay, backup and rehearsal never mutate cards. Backup
keeps them; transfer excludes them so the target grants its own paths.

## Team initialization and enrollment

`rcp space init --team` needs an interactive terminal and explicit name, creates
a team and prints one bootstrap code once. Fresh installed service stays stopped
until initialization as `rcp`; serving never prints a secret or initializes beside
another lock owner. [Install](../server.md) owns this procedure.

The first member redeems bootstrap; later members redeem short-lived single-use
invitations from any member. Repeated bad guesses lock that code only. Copy text
names space/expiry and shows the code once, never in a URL. The identity panel
shows an equal-ranked roster/count and creator-only invitation ledger: waiting,
joined name, expired, locked or revoked. Refresh rereads both.

Only a live invitation's creator may revoke it, idempotently preserving the first
time. Redemption reports it as invalid, not revoked. Consumed invitations cannot
remove members. Restore and member removal revoke outstanding invitations.

Enrollment returns one high-entropy permanent `rcp_` token; SQLite keeps indexed
SHA-256 hashes and uses constant-time comparisons. Raw tokens go only to controlled
exchange, never URLs, ordinary requests, JavaScript storage, prompts or history.
Exchange creates an HTTP-only Secure SameSite=Lax `__Host-` browser cookie with
central idle expiry. Restart does not reenroll. Members rotate/revoke only their
own token and log out their own sessions, subject to last-member safeguards in
[machine operations](server-and-machine-operations.md#machine-authority-and-the-operator-surface).
Revocation does not cancel already-authorized tasks.

Second-device pairing exchanges a short-lived single-use code plus human-entered
device name for its own session, not the permanent token. Restore revokes pairing
codes. Desktop **Add team space** uses SSH to loopback, then bootstrap/invitation
or permanent-token exchange. Phones use tailnet HTTPS per the
[device guide](../device-pairing.md); public HTTPS is not a deployment path.
Unauthenticated team browsers see focused sign-in; personal spaces use owner auth.

Each connection proves current native protocol overlap; the
[projection spec](api-web-and-desktop-projections.md#confirmed-team-desktop-target)
owns ranges and fixtures. **Why:** version age and saved minima cannot prove wire
compatibility. A small integer range avoids a second feature-capability API.

## Project identity and home

Every project carries a random durable `project_id` and current `home_space_id`
in canonical Patch history. Creation or legacy adoption appends one visible
system-produced identity revision. The id never derives from name, host, or path
and never changes.

Exactly one space is the writable home. A backend whose `space_id` differs from
canonical home refuses ordinary registration/writes instead of inventing a
second identity or read-only catalog mode. Low-level replay stays space-neutral
for recovery and never consults local membership or admission data.

The desktop supports a one-way personal-to-team transfer and switching between
enrolled spaces; there is no fresh-identity fork. Transfer
creates a separate team checkout and moves the canonical home only after the old
home is fenced. Its home-change record preserves both space-scoped human actors;
it does not change ownership of a person's checkout. Team-to-team and
team-to-personal product transfers remain excluded. The [transfer archive](server-and-machine-operations.md#personal-to-team-transfer-archive)
owns transferred history and recovery.

Episode archives travel with their original archiving-human snapshot and time.
A graph branch that Merge cleanup archived travels as its owner's
`graph_archived` flag; the target records a graph-only isolation with that flag,
and the source's worktree binding and merge state stay behind.
Operational-record readers accept versions 1 to 7; writers use the lowest that
fits: version 1 with none of these, version 2 when episode archive metadata is
present, version 3 when a graph branch is archived, version 4 when the
project holds [branch-name](auto-research-and-branch-merge.md#branch-names)
reservations, version 5 when a member deleted an episode report, version 6
when the project holds graph branch records, and version 7 when it holds a saved
Paper, which travels with the transfer. Older records and source
configurations keep their original serialized shape. The optional
`record_schema_version` source-configuration field makes older strict clients
and targets refuse preparation before the source is released. Release rechecks the required version under the storage write lock,
and archive changes obey the same transfer fence as other project writes.

Every project has exactly one canonical state repository, local or remote. Its
main and Auto-research graph-branch namespaces are parts of that same canonical
repository; a graph branch does not create another project home.

## Project membership and invitations

Notification preferences belong to a member and project. Devices belong to the
space: a personal desktop device belongs to the local owner, and a team desktop
device belongs to its registering session. Logout, session revocation, expiry,
and member removal detach the affected team devices. Delivery does not extend
session expiry. Before every delivery or retry, current membership and the
current kind preference must still permit the item; otherwise its queued row is
dropped. An expired owning session deletes the device and its queued rows.

Joining a team space does not join its projects. Project membership stores
durable `user_id`s operationally in SQLite and never enters `.research`.
Creating a project seats the creator. Legacy/memberless projects are claimed by
current members at registration or by the first later enrollee so the catalog
cannot produce an unrecoverable invisible project.

Any project member may invite any enrolled space member. A project invitation is
an authenticated in-product item carrying no credential and cannot enroll its
recipient in the space. Pending invitations appear on the project index because
the project Inbox is unreachable before membership. The recipient may accept or
decline.

Every member has the same project role. Settings shows membership, **Invite
member**, and **Leave project**. A person may leave their own project only while
another member remains; the last member must add another enrolled project
member first. A last member may instead explicitly delete the project from RCP;
that confirmed catalog operation is distinct from leaving and does not
deprovision a team checkout or Git credential. Losing membership applies the
durable Stop fence to further project work while an already-authorized turn
settles honestly.

Every project-scoped route checks membership before revealing existence, and
Apply checks again under the canonical append lock. A nonmember project is
absent from project lists, the cross-project Experiment board, and direct lookup
in exactly the same way as an unknown id.

Membership is authority, not disk confidentiality. The canonical repository
still has whatever read visibility its host operating-system account permits.

## Project groups

A project group holds related projects that each need their own research graph,
for example one project per dataset of a benchmark. A group is space-local
operational state: the `project_groups` table and a nullable `projects.group_id`,
so a project belongs to at most one group. Nothing about a group enters
canonical history, replay, or project identity.

A member sees a group when they belong to one of its live projects, or when they
created it and it is still empty. Any member who sees a group may rename it, and
move a project they belong to into it or out of it. Nothing about groups depends
on the space kind: project creation is unchanged, and **New project** inside a
group returns to the group's shelf, which files the created project there with
the same move and then opens it; a failed move leaves the project ungrouped and
the shelf header's retry repeats it. Only an empty group
can be deleted. Deleting a project, or transferring it to a team, leaves
the group; a transferred project arrives ungrouped. Backup and restore carry
groups with the rest of SQLite.

Grouping widens what agents read, never what they write: see
[group read scope](providers-and-containment.md#group-read-scope).
**Why:** RCP members are one lab, so a group shares reading freely; the one
boundary that matters is that each run still changes only its own project.

## Member terminal lifecycle

`TerminalManager` admits one session per project/repository and one shell per
resolved working tree across aliases/projects. Tree identity is resolved path
plus host, not declaration spelling. An opening request reserves that identity
so renames cannot admit another shell. Project members can attach/end their
session; its starter remains the audit actor. Lost starter membership, account
removal, project deletion or retirement ends it; viewers losing access disconnect.
A slow viewer detaches with reconnect available while the shell keeps running.
Mouse-reporting programs receive drags; Option-drag on Mac or Shift-drag elsewhere
still selects text.

Sessions survive view closure/browser disconnect. Only arriving input renews the
30-minute idle lifetime, even if PTY delivery is backpressured. Output and passive
attachment do not. A five-second sweep checks membership, exit and expiry.
Shutdown/maintenance ends sessions; mirrored startup reconciles orphan units and
persisted launch intents before new sessions.

A mirrored record finishes only after its unit is confirmed gone, including
failed launches or exited SSH. Remote cleanup uses a fresh connection if needed
and the recorded account. Shutdown leaves uncertain records unfinished without
waiting serially for the network. Failed live stops retain runtime for retry,
hide the session, disconnect viewers and discard their queued output. Successful
stops drain queued output. Any uncertain stop blocks a second shell on that tree,
even under a new alias.

Startup reconciliation is bounded and concurrent, best-effort per record, never
a server boot gate. Metadata must include every current field with correct string
or end-field null types and a filename matching session id; defaults cannot turn
missing host into local authority. Unreadable records stay untouched and block
any identifiable project/repository. Unexpected reconciliation failures also
block. Each filename owns its independent blocker. Readable unfinished records
are retained before stopping, including when the startup budget expires. Foreign
data-directory unit identities retire as mismatches without attempting a stop.
Open retries every retained record matching alias or resolved tree and refuses
until all owned units are confirmed gone; unreadable records can match only alias.

Sessions pin declared path, machine, host and account. List, open (before 404 or
launch checks) and socket access retire changed/removed registrations. Stale
aliases retire concurrently; open settles only its own alias. Failed retirement
retries later and keeps the tree reserved. An unchanged running session returns
before capability/inventory checks, whose failure gates only new launches.
Probe, resolution and launch run outside the manager lock; duplicate concurrent
opens refuse rather than queue behind an unreachable machine.

Backend eligibility belongs to the repository machine, not space kind. Linux
requires systemd tools, `findmnt` and a reachable user manager for mirroring;
other systems use cooperative PTYs. Terminal capability does not require linger.
Cooperative termination hangs up then uses bounded kill; deliberately detached
descendants have no cleanup guarantee or canonical-state filesystem fence.
Payloads explicitly report that limitation. Mirrored verification failure never
falls back to cooperative.

One source-shipped capability probe per machine is cached for the manager's
lifetime, failures included. Misses project pending and run in background.
Metadata changes or terminal Refresh invalidate it; stale in-flight probes are
cancelled and cannot replace newer results. Remote sessions use an SSH PTY;
link loss ends the session without replacement. A completion marker distinguishes
shell exit 255 from transport exit 255. Launch/containment details belong to
[Providers and containment](providers-and-containment.md#member-terminals).

Metadata records member, project, repository, start/end and reason, never a PTY
transcript. A bounded memory tail serves reconnect and disappears on end; no
shell history is written. Terminals add no graph mutation or manifest permission.

## Repository and truth scope

The project manifest names repository aliases, paths paired with execution
machines/hosts, truth-scope membership, provider roots, and execution profiles.
Project truth scope is human-authored canonical membership, and the graph's
`project_truth_scope` is its one source: `agents/project_repositories.py` reads
it for every launch, and the manifest's `project.truth_scope` is only a mirror
the history manager writes, as is the legacy `agent.default_run_truth_scope`.
Every agent reads every project repository; the truth scope names the ones
Work-like runs may write. Settings' **Agents can write** box on a repository row,
and the same box when adding one, is that membership: toggling it appends one
human approval `set_project_truth_scope`, never a manifest edit. There is no
per-turn repository selection. Admission stamps each task's `run_truth_scope`
with the whole truth scope, except that a worktree (a chat's, or an Experiment's
or Auto-research start's code worktree) names its one writable repository on the
run machine. An Auto-research start that names `worktree_repository` turns its
code worktree on for that repository. Older
tasks and Patches keep the narrower scope they recorded: a resume, retry,
continuation, Experiment watcher start or Auto-research worker keeps its
authorized write set, never the current truth scope, while still reading every
repository, and old records replay unchanged.

A personal project adds a repository directly with
`POST /api/projects/{id}/repositories`: a Git work tree root with at least one
commit on the base machine, overlapping no project repository, published as the
same human approval a team add completes with (descriptor plus truth choice).
Personal projects have no clone path, so their Add repo form offers only a
folder; team projects add through a setup request, below.

A repository's alias is its only identifier, so the Web calls it the name.
Setup suggests it from the source: a chosen folder's name, a GitHub
repository's name, else the project name for an RCP-created repository, else
`research`. The suggestion is slugged to `[a-z][a-z0-9-]{0,47}` and numbered
`-2`, `-3` past names already in the project; it follows the source until the
human types a name. A project with one repository shows no name field in the
setup wizard, so its name is always derived; the field appears once a second
repository makes the names need to differ. Existing aliases never change.

A Patch declares the repositories its run read in `repositories_read`, each by
ref: a bare alias names this project's repository and `<project_id>:<alias>` a
group sibling's (`agents/project_repositories.py` owns the encoding). Task
contracts name repositories by path, so RCP accepts a ref, this project's id with
an alias, or a path at or under a repository root shown to the run, and records
the ref. RCP stamps the run's resolved read scope on the collected Patch
([Patch provenance](graph-history-and-transitions.md#patch-provenance)); a read
outside it is rejected. A Patch may cite any project repository as
`source_refs`.

Every graph-capable run, the Paper coach and group-context siblings receive
pointers to every project repository. Work-like write containment separately
permits exactly the admitted writable repositories on the execution machine,
checked again at launch against the graph's current truth scope; context and
write permission are not the same thing.

Provisioning storage represents a repository source as a GitHub reference or
null (server only). Requests can create a team project, receive a transfer, add
one repository, or connect one existing repository to GitHub. Add and connect
name `target_project_id`; they share its checkout namespace without consuming
the unique project reservation held by create/transfer requests. An add clones a
GitHub source onto the project's base machine, or adopts an `existing_path` there
in place: a Git work tree owned by the machine's account with at least one commit,
whose intended and resolved paths are that folder and whose code, like any
server-only code, is not backed up. It retains the human's
`count_as_project_truth` choice, defaulting to false;
persisting this choice does not itself change canonical truth membership. Creation
captures an immutable hash of the current canonical manifest and graph head,
keyed by the request identity bound into the final review digest. The requesting
member's final confirmation checks this boundary under the canonical workspace
and history locks. Add publishes one human approval transition containing the
repository descriptor and the chosen truth scope, including descriptor-only
additions outside truth. Its request identity reconciles a committed approval
if the operational completion receipt was lost. Connect changes only completed
checkout provenance, never manifest or graph membership. Both require current
project membership; a changed review boundary requires a new request.

`effective_repositories(project, provisioning_requests)` owns the effective
repository inventory. It uses only completed requests for the exact project
and home space, validates their review digests, and folds one create/transfer
request followed by add/connect requests in completion order. Adds append an
alias; connects replace its source, deploy-key evidence, and checkout proof
while preserving its truth choice and checkout location. Incomplete proofs or
conflicting provenance are refused. GitHub entries retain deploy-key and write
evidence; server-only entries have checkout commit/time evidence without a key
or push proof.

## Durable agent task lifecycle

`AppStore` uses SQLite; compound transactions address harmful partial writes,
not aesthetic module size. Each thread reuses one connection for outer blocks;
nested blocks commit independently. Acquire resets transaction, foreign-key,
row-factory and migration-pragma state. Memory databases are uncached. Idle handles
are globally bounded and close at thread exit, disposal or `close()`; active
blocks close on exit. Shutdown and temporary stores close explicitly. Inode
replacement invalidates cached connections. Live snapshots use SQLite backup;
offline copies/hashes/publication close stores first to flush WAL.

Empty databases replay current raw DDL captured from a real migration, in SQLite
creation order, stamp the ledger and seed space/owner/bootstrap identity.
Existing databases run ordered migrations and validation.

`queued` can become `running`; `queued` or `running` can become `pausing`.
Any active state can become `paused`, `succeeded`, `failed` or `interrupted`.
Resume/Retry admit child attempts under chain rules, never rewrite parent status.
Each change checks and updates in one write transaction. A refused single-task
transition preserves status, receipts, Patch, notices and cleanup, adding one
truthful warning. Human pause instead retains its explicit error without a refusal
event; bulk restart ignores terminal rows. Missing ids raise `KeyError` without
side effects. Terminal progress updates quietly do nothing. Cleanup and lifecycle
receipts/notices occur only for an applied transition.

Public rows supply `active`, `queued`, `pausing`, `awaiting_human`, `paused`,
`failed`, `settled`, `finished`, `status_label` and retained status. Web gates and
labels use those answers; its sealed response type prevents a second status
machine in client string branches.

`storage/request_compat.py` normalizes persisted requests when a row becomes an
`AgentTaskRecord`, before any recovery/Retry/watcher/mail policy parses it.
Only the explicit per-kind retirement allowlist removes fields (currently legacy
`auto_research.ending`, and the per-turn compute selection that every
RunRequest kind froze through 0.4.29). Mappings assembled outside row decoding use the same helper. `RunRequest` and
`CoachRequest` drop unknown fields when parsed, so the roundtrip rule below is
what refuses a stored row that carries one; other live request models are
`extra="forbid"`. Retire only shipped fields whose removal preserves meaning.
Every launch and finalization check uses the one roundtrip rule in the same
module. Dumping the parsed request must reproduce the stored row. A field the
row predates may be absent, at any depth, when it parses to its default.

The [backend ownership rationale](../../AGENTS.md#cross-cutting-implementation-rules)
explains why named engine/owner calls do not require a plugin architecture.

## Add project and retained research

One project wizard exposes backend-authorized intents: **Use an existing checkout
personally**, **Create a shared team project**, and **Move an existing personal
project to a team**. Entry may preselect intent/source, including Settings and
project-card Move; heading, review and progress name it. Personal setup uses the
direct owner, team creation uses provisioning, and move coordinates both spaces.
Team backends refuse old direct registration/setup routes before interpreting
bodies or inspecting files. Backend eligibility, not path or space-kind inference,
decides which fields/actions appear.

Personal setup accepts local/SSH paths. Desktop offers its native local picker;
SSH browsing starts at the authenticated user's home, one backend-validated
level at a time, labels Git/`.research` directories, never recursively scans or
collects credentials, and retains manual absolute entry.
Team setup derives managed paths from GitHub sources or blank **Server only**
choices. Empty GitHub setup announces its initial commit. See
[Central checkouts](server-and-machine-operations.md#central-checkouts-and-repository-credentials)
for sources and [provisioning](server-and-machine-operations.md#durable-project-provisioning)
for review. Move reads source identity/history and asks only for target changes.
Desktop offers it only when source export, target import and native authenticated
relay are all available; browsers have no native relay capability.

An existing `.research/manifest.toml` is retained research regardless of the
entered name. Read-only preflight replays without repair, publication, identity
claim or catalog changes and shows canonical identity, location, revision count,
compatibility and exact failure. Compatible state opens without manifest overwrite;
incompatible state may expose its last coherent materialization read-only without
claiming home or repairing history.

**Start fresh** requires exact human review. Under the local/remote append lock,
it rechecks manifest/history fingerprints and atomically renames all `.research`
to a unique timestamped sibling. Drift requires review again; incomplete archive
proof prevents initialization. Later failure leaves the archive recoverable and
no broken registration.

Registered legacy projects adopt their current space idempotently; separately
discovered state needs explicit claim. Catalog aliases, tasks, chats, papers,
watchers, attachments and display caches move atomically to the canonical random
id. Interrupted migration converges without duplicate identity revisions or moving
a native session workspace.

## Rebuildable caches

Project-owned remote-source copies, session slices and display caches are bounded
and noncanonical. Clearing affects only the selected project and is blocked by
its active readers. It deletes source copies/slices and rebuilds the display
snapshot because that is also the offline copy; failure retains and names the
previous snapshot. Every snapshot uses the running release's official skill
catalog and defaults for unset manifest fields, never a cached catalog.

Personal spaces also offer explicit app-wide cache clearing, blocked by any
project reader. Teams offer only project clearing because membership does not
authorize other projects. Neither action touches provider originals, repositories,
canonical state, tasks, chats, drafts, views or Paper.

## Process and lock ownership

The [instance invariant](../../AGENTS.md#stable-invariants) is enforced by an OS
lock. `rcp open` reuses a healthy owner or gracefully replaces an unavailable one;
`serve` takes over only after recoverable work pauses. The old server gives
requests bounded grace shorter than takeover wait, then aborts canonical-lock
waits. No manual process hunting is required.

Remote writers wait on process-held advisory locks; refresh waits are bounded.
Display-only episode/Experiment/Home/branch polls accept ten-second mirrors;
decision reads use two seconds and writes sync first. Process death or missed
client heartbeats releases the holder within its timeout. Only proven-empty
legacy lock directories may be reclaimed; populated, symlink or special entries
remain with an exact diagnostic, never a deletion instruction.

## Deletion

Deleting a personal or team project first validates every app-owned file target
without changing it. The SQLite transaction then repeats the active-work check
and removes the registration plus all project-owned control-plane rows. Only
after that commit does RCP remove task stages, snapshots, rebuildable caches,
and registered imported provider history. A cleanup failure is logged with the
project and path but does not restore the registration or report deletion as
failed; any leftover app files are inert.

Delete stops in-flight work instead of refusing because of it. Opening the
confirm dialog reads `GET /api/projects/{id}/deletion-preview`, which names
what will stop: active agent tasks (including branch merges and run reports),
live runs, running compute jobs, and live watchers. The dialog renders those
lines verbatim and its action becomes **Stop work and delete**. The action
stays disabled until the preview loads; a failed load gives the dialog a red
edge and a retry. Delete sends back the preview's `stop_token` and stops only
work the human was shown: inside the deletion fence, if anything is left to stop
and it no longer matches the token, Delete refuses with 409 before stopping
anything and the dialog reloads the list. Work that finished meanwhile never
refuses. Confirming
first runs the non-destructive refusals (an unfinished transfer, unsafe file
targets), so a refused Delete stops nothing. It then stops everything inside
the catalog's deletion fence, which already refuses new work for the project.
First it retires the watchers so none wakes a turn.
Then it presses each run's Stop, without a report. Then it stops every agent
task the way Pause does, killing the local or remote provider, and waits for
the workers to exit. Last, it runs the saved cancel action of each running
helper job and of each watched external job. A cancel's success only means
the command was accepted, so Delete re-runs that job's saved check for a short
bounded time until it stops reporting running. A process RCP cannot confirm
stopped, such as one on an unreachable host, whose cancel failed, or whose
check still reports it running, does not block deletion. A watched job saved
without a cancel action is checked once and listed if it still runs. The
response lists each in `unstopped`, and the Web shows those lines once the
project is gone. Only an unfinished project transfer refuses
Delete, from the preview onward. The transaction still refuses a task that
outlived the stop, for example one admitted mid-stop, and Delete refuses while
a stopped task's worker thread is still running past the wait, since it may
still use the project's rows and stage; retrying Delete finishes the job.
**Why:** a refusal named work the human often could not see, such as a
chat-launched job, and made them hunt for it; the project is going away, so
its work has no later owner. Deletion never edits
the underlying research repositories or canonical state repository. For a team
project, it also leaves the server-managed checkout and Git deploy key in place.
Removing either is a separate operator deprovisioning action. The backend
publishes the exact confirmation consequence on the project card, and the Web
renders it verbatim rather than inferring policy from space kind or paths. For a
team-shell protocol-1 request, the project-card list preserves the older
contract: `can_delete=false`, `delete_unavailable_reason` explains operator
deprovisioning, and `delete_confirmation` is absent. Protocol 2 and callers with
no protocol header receive the deletable card with `delete_confirmation`, but a
team `DELETE` itself requires protocol 2 or newer.

## Bulletin operational storage

Migration 57 owns target-local Bulletin posts, command receipts, member read
cursors, and durable mention-notification observations. Posts retain their
original agent roster actor id across turns, parsed mentions, edit timestamps,
and soft-delete timestamps. Artifact links are not stored: each read resolves the
text's `<artifact:ID>` tokens against the project's readable artifacts (the
artifact store's one availability rule), so a token naming no readable artifact of
the project stays prose. Posting or editing a post that links an unsaved artifact
saves it in the post's own transaction and under the artifact's lock, so a link
never outlives its artifact. Saving settles no review: a linked consolidation
report stays open in the Inbox. Membership, author identity,
receipt lookup, Stop admission, and mutation are checked within the storage
transaction. These rows
are operational records and do not mutate canonical graph history. Restore
schema fingerprints, backup integrity, and resource-version registries include
the migration's tables and triggers. Each target keeps at most 200 posts by
evicting whole threads, least recently active first, so an accepted post is never
the one evicted. Post retention also prunes observed mention
rows whose posts no longer exist; unobserved rows remain for reconciliation.

A roster actor is one board conversation: `chat:<chat_id>` for chat and experiment
turns, `worker:<worker_id>` for child Work conversations, and
`episode:<root episode id>` for orchestrator chains, including their artifact
edits and later human turns. Repeated experiment episodes in one chat share one actor,
so the Bulletin roster and Agents board identify the same agents. Archived
conversations and seated Auto-research workers without board conversations are
excluded. Historical posts retain their recorded actor ids. Only eligible
ordinary chats, Experiment conversations, child Work chats, and orchestrator
chains hold the once-only `name` command; seated workers and Graph consolidation
do not. The [launch contract](providers-and-containment.md#bulletin-commands-and-context)
owns eligibility and enforcement.
