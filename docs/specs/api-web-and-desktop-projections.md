# API, Web, and desktop projections

This specification owns public project projections, current Web surfaces,
revision reconciliation, navigation and tab state, and desktop-shell lifecycle.
It does not grant graph authority; mutation routes delegate to the state
workspace and transition manager.

## Resource watermarks

`POST /api/resource-watermarks` takes `client_id`, the complete `open` project
set with refs and resource keys, `due` project ids, and optional `activated` id.
It resolves aliases and membership before reading counters or renewing sync
demand. Unknown and forbidden ids both appear in `unavailable`; only due projects
receive tokens. Responses carry `epoch`, one `as_of`, per-resource keys (`name`,
optional `ref` and `entity`) with `version` and `valid_until`, plus remote sync
health.

Set-based SQLite reads cover authorization/aliases, transactional counters,
indexed deadlines, and, when progress is watched, which projects have a running
task. The route never opens a project, replays history, completes a snapshot, or
waits for snapshot locks or SSH. File families use published catalog
generations; terminal runtime uses its owner's generation. Graph tokens fold in
whether sync health reports an error, because the graph payload carries
canonical reachability. While a task runs, `tasks.progress` and task details
take a clock tick, because elapsed time and progress are computed from the clock.
Entity counters are project-scoped; chat read/pin counters are member-scoped.

A request with `agent_threads: true` also gets `agent_threads: {version}`, the
caller's own counter, read without any project id; otherwise the field is null.
Triggers bump only the owning member on that member's thread rows, thread turns,
reads, names, browser choice, thread questions, approval cards, schedules and
delivered handoffs. A file-backed transcript append publishes through its turn's
task row. A changed version reloads the thread list and the open thread.

`tasks.status` and chat, episode, and terminal inventories exclude progress;
`tasks.progress` and entity details include it. Terminal input renews idle expiry
without invalidating inventory; retirement does invalidate it. Question liveness
renews its deadline without advancing the counter until it returns to waiting;
publication failure is logged without failing the ask. Experiment runtime and
report changes invalidate graph controls; unrelated episode ledgers do not.

Tokens include the latest elapsed deadline. `valid_until` is the next boundary
for finished-chat visibility, question liveness, artifact expiry, consolidation
authorization, or readiness. `epoch` fences restart, restore, and projection
contract changes. Payload shapes stay independent of watermarks: a read commits
the token observed before it started, so a change during the read triggers another.

Watermarks alone renew remote-sync demand: membership-filtered client leases last
60 seconds and are unioned by canonical workspace root. The coordinator captures
open remote projects every 10 seconds without overlap; activation bypasses cadence
and coalesces a follow-up when busy. Local projects bypass it. One-shot requests
inside cadence remain scheduled for its end.

Captures stage privately under the remote refresh lock, reuse unchanged files,
validate, sync files and descriptors, then atomically publish an immutable
generation. Reads pin it until finished; capture and write publication share
sequencing so an older capture cannot replace a newer commit. Invalid display
inputs retain the last good generation with sync error. A torn trailing chat
record waits for completion. Unchanged captures update freshness only; a busy
canonical writer skips a round without reporting outage.

Display services, graph snapshots, readiness, and digest reuse the generation's
replay; current owners supply operational controls and sync health. Cold reads
schedule capture and return loading/unavailable, never await transfer. Writes
still validate canonical state; repository ownership reads the canonical manifest
under the writer lock. Display publication failure cannot undo a canonical write.

## Project graph-ref inventory

`GET /api/projects/{project_id}/graph-refs` returns main first, then one row per
published branch record, episode branches and forks alike, archives included,
newest first (an owned branch by its newest chain member, a member fork by its
own creation), under project membership. A fork reserved but not yet published
has no row. `POST .../graph-refs` and `POST .../graph-refs/{branch_id}/merge`
fork and merge forks as described in
[Forks](auto-research-and-branch-merge.md#forks).
`GET .../graph-refs/{branch_id}/merge-preview` returns that fork's `MergePreview`.

Rows carry `kind`, `branch_id`, `head`, `base_head`, `episode_id` (chain root),
`current_episode_id` (newest member), graph-isolation `archived`, and merge fields
`merge_state`, `merge_eligible`, `merge_blocked_reason`,
`latest_successful_merge`, `active_merge_task_id`, and `merge_diagnostic`.
Branch rows also carry `parent_head`, and branches carry the root's `mode` (null for a
member fork), `created_at`, resolved `name`, `running`, and `needs_you`; there is
no `title`. Running means an active task on that target; attention means an open
Decision/Blocker or answerable question. Names use the shared SQLite owner with
date fallbacks. On an episode branch, `branch_id == episode_id` names the root.
Main has its exact head, name `main`, false flags, and null branch/episode/base/
merge details. Merge state reads every merge task out of the branch, whoever
dispatched it; eligibility uses the isolation reservation and writer checks.

`PUT .../graph-refs/{branch_id}/name` takes `{"name": str}`;
`PUT .../graph-refs/{branch_id}/archived` takes `{"archived": bool}`.
Both admit project members and return the GraphRef. Rename returns 422
`branch_name_invalid` or 409 `branch_name_taken`; unknown branches return 404.
Main cannot be renamed or archived. Archive updates the branch record and mirrors
the flag into isolation state only for an episode's own target branch, creating
that state if missing. It changes neither Patch history nor episode-history archive state.

The `⑂ <name>` picker sits after view tabs, or beside the project name on phones,
even on main with no branch. Its last row, New branch, opens a name field (empty
lets the server choose) and forks the viewed head through `POST .../graph-refs`
with `{request_id, at, name}`, then switches to the new branch. A refused name
(`branch_name_taken`, `branch_name_invalid`) reddens the field; any other failure
reddens the row and offers one retry icon, reusing the request id only after a
network or 5xx failure. The artifact viewer's pin picker offers no New branch and
hides on main with no live branch. An amber dot signals attention on another
unarchived branch. Rows list main first, then each parent before its indented forks, running then
newest among siblings. Eligible forks offer Merge after a confirmation. Rows hide archived branches
except the active one, which offers Unarchive. Rows show running/attention marks,
rename/archive icons, and a check for selection. Rename is inline; archiving an
unmerged branch asks first. A failed list disables the picker but retains its name.

Switching ref keeps the view, valid destination-owned chat/episode selections,
and nodes present in the new snapshot. The mounted previous snapshot stays inert
under a progress line until the new one arrives; only a first project open uses
an opening screen. Cached refs paint before validation. Each ref retains its draft;
resource ownership and subscription rules are in [Revision observation and drafts](#revision-observation-and-drafts).
One URL helper owns project/ref/view plus chat or episode; exact Experiment routes
retain their separate `experiment`/`target`/`branch` fields.

## Personal owner admission

Personal API requests, API documentation, and terminal upgrades require an owner
session. Public entrances are the Web shell, GET/HEAD health, owner exchange/code
redemption, and OPTIONS. Public personal health exposes adoption identity only;
team health retains runtime bootstrap fields. The separate notify-only phone
listener grants no owner access. Owner cookies are host-only, HttpOnly,
SameSite=Strict, and Secure on HTTPS; team cookie policy is separate.

The Web reads identity before protected boot data. A 401 opens sign-in, accepting
a pasted one-time code or URL fragment. Project locator intent survives sign-in
and needs human confirmation before registration. Display-name entry is separate.
Authenticated `/api/health/details` supplies runtime and project-creation data.
A native owner-session 401 clears the cached session and returns the verified
identity with `owner_authenticated=false`; Quit and Update still require
protected active-work counts.

Personal mutations check Origin. Simple POST content types, including absent
Content-Type, return 415 `personal_simple_request_refused`; bounded multipart
chat attachments and Paper uploads are the exceptions. JSON and transfer-proof octet streams remain
admitted. Session provisioning belongs to
[personal sign-in](projects-spaces-and-operations.md#personal-sign-in).

**Why:** Loopback is reachable by local agents and web pages; network location
cannot grant human approval authority. API sessions close accidental and trivial
access paths, but do not isolate credentials from determined same-account code.
No agent launch receives an owner credential.

## Machine hidden folders

The machine payload includes persisted `hidden_folders` and computed `hidden_read`
shaped as `MachineHiddenReadProjection`: `default_paths` (code-owned defaults; a
remote machine's paths are `~/` display templates), `user_folders`, and
`readiness`. `readiness` is a `HiddenReadStatus` (`enforced` or `unhidden` with
stable reason codes, including key and browser gaps) for the backend's own
machine, and `null` for a remote machine, whose status is checked at launch.
The resolved per-launch `HiddenReadScope` is not projected. Defaults and status
are read-only projections, not writable settings.

The existing `PATCH /api/space/machines/{machine_id}` accepts `hidden_folders`
as a whole replacement list. Omission preserves the list; an empty list clears
only user additions. The same member admission as writable paths applies.
Validation runs on the execution host and delegates overlap policy to
`validate_machine_hidden_folders`; refusals return HTTP 422 with
`detail.code` and `detail.message`. A refused edit changes none of the card's
fields. Launch-time validation remains necessary after a successful save.

## Agent browser Web consumers

The chat Options menu reads and updates `/api/projects/{project_id}/chats/{chat_id}/browser`
with the `browser_requested` preference. The strict PUT body contains only that
boolean. The preference exists before the chat has messages. Turning it off
deletes the chat's browser profile after the response; an active turn keeps its
browser until it ends. A failed write
triggers a read to reconcile the server value before another change.

Experiment launch requests and Auto-research episode starts send
`browser_requested`. Episode responses expose the persisted preference.
Chat transcript messages and episode tasks expose `browser_status`; the Web
renders unavailable and lost status on the corresponding turn. Experiment
Runs loads its exact episode alongside the timeline for those task statuses.

Machine cards independently GET `/api/space/machines/{machine_id}/browser`.
Their explicit Install action POSTs `{}` to that path's `/install` endpoint
and replaces readiness with the response. These calls never block the machine
list or the rest of a card. Unknown readiness and reason codes retain the
server detail in a generic failure notice.

## Member terminal API

Project terminal routes under `/api/projects/{project_id}/terminals` are:

| Route | Contract |
| --- | --- |
| `GET /repositories` | Repository/start path, machine capability, running Work identities. |
| `GET` | Open sessions and live/idle state. |
| `POST /probe` | Invalidate machine probes and schedule fresh results. |
| `POST` | Open or return the repository's session; `require_new: true` returns 409 if one exists, under the manager lock. |
| `DELETE /{session_id}` | End the session. |
| `WS /{session_id}/ws` | Binary PTY output; JSON `input` (`data`), `resize` (`cols`, `rows`), `ended` (with a `reason` when the SSH link dropped), and `detached` (`reason`) when output falls behind. |

HTTP checks membership. WebSocket upgrades independently check identity,
membership, origin, and maintenance, then recheck before input, periodically,
and at a short interval before output. Revocation closes the socket. Unknown
and nonmember projects are indistinguishable. Output checks are interval-based
because synchronous store reads per PTY frame would be costly.

Repository rows expose `eligible`, `unavailable_reason`, machine/backend identity,
`containment`, `os_name`, `probe_state`, and `reason`. Available machines are
`mirrored` or `cooperative`; unavailable ones have null containment. Remote states
are pending, reachable, incapable, unreachable, authentication_failed, and
host_key_failed. Cold reads schedule one probe per machine and return pending;
Refresh or changed machine metadata invalidates the cache. Projection and launch
share capability resolution; mirrored launch failure never downgrades silently.
Space kind does not decide eligibility.

Sessions expose `containment` and `protection_notice`. The destination remains
visible for capable machines, open sessions, or pending/failed remote probes.
Empty projects and unavailable-local-only projects without sessions hide it.
SSH loss ends and removes a session; its diagnostic survives refresh in the open
view, but not departure. Elsewhere, the reason remains in its persisted record.
Opening again creates a new shell, never a silent reconnect.

[Providers and containment](providers-and-containment.md#member-terminals) owns
machine capability and canonical-path refusal; [terminal lifecycle](projects-spaces-and-operations.md#member-terminal-lifecycle)
owns expiry and metadata; [Terminals](interface-and-visual-design.md#terminals)
owns layout and the cooperative-session warning.

## Since you last looked

The pull-only digest follows the open graph target and is private to a member.
`GET /api/projects/{id}/digest` reads main; `?branch_id=` reads an existing branch
or returns 404. It returns `cursor`, `mark`, `target`, `needs_you`, `changed`,
`reports`, `changed_node_ids`, and rendered-line `count`.

There is one member mark across targets. Only main offers Caught up, so a branch
cannot acknowledge unseen main lines. Reads never advance a mark. After the
projector baselines main, existing branches, and episodes, a new member starts
at the current event cursor with an empty digest; project cards do not create
marks. Before that checkpoint, GET signals projection and returns `mark: null`,
`cursor: 0`, and no lines; Caught up is 409 and landing counts are zero.

`POST .../digest/caught-up {"seq": ...}` acknowledges only the displayed cursor,
returns `{mark: {seq, marked_at}}`, and never moves backward. Missing marks or
future cursors return 409. Later events remain new; sequence high-water survives
project deletion. This action skips project work admission but keeps maintenance,
membership, origin, and JSON checks. Overview shows `marked_at` in browser time.

Needs you contains the target's new, still-open Proposals, ready/revisit Decisions,
answerable questions, and episodes needing a human. An Auto-research chain is one
branch-named item on main while it needs attention; on its branch the questions,
Proposals, and Decisions appear individually and the run stands for its
orchestrator. Children never become items or surface their parent. Standalone
Experiments use node names. Archived branches contribute no attention.

Changed groups semantic changes by source, assigning each node to its latest
eligible source. First exclude the viewer's direct edits and requested Work turns
already reached by their chat read marker; earlier eligible touches remain.
Unseen completed turns remain until the chat is read. Provider authorship is
unchanged. Removed nodes and changed edge endpoints participate. Main lists only
main changes; unmerged work appears on its branch. `changed_node_ids` includes
only surviving main nodes and is empty on branches. Reports opens the target's
episode/consolidation artifacts; a report whose artifact was deleted leaves the
list and the count. Jobs, failed tasks, ended episodes, memory notes,
and chat messages alone are not lines.

Links open ordinary in-app routes: a run's branch orchestrator, exact Experiment
Runs entry, Inbox item, or consolidation report. Attribution precedence is merge,
consolidation, identified human, ingestion, captured Work, other agent, system,
then unattributed human. Stable source keys do not depend on display titles or
optional task enrichment.

SQLite owns the append-only commit-ordered events and marks. Operational events
commit with their source writes. A background projector follows accepted history,
repairs missed signals, and never writes canonical state. A non-prefix restore
rebaselines that target with one transactional reset; attention and graph-change
rows at/before that reset are ignored, while operational events remain. Requests
never replay or project history, and notification sender state is independent.

Project cards use the same main grouping, bounded by `DIGEST_LANDING_EVENT_LIMIT`;
counts are floors for truncated backlogs and zero for unmarked projects. Full
digests are exact. Events, marks, and projector heads are backed up/restored,
excluded from project transfer, and deleted with project control-plane data.

## Desktop notification delivery

The backend exposes a desktop delivery target through
`POST /api/notifications/devices/desktop`. Personal registration belongs to the
local owner; team registration belongs to the calling member's current session.
Registration is idempotent for that owner/session. A member cannot pull or
acknowledge another session's device, including another of their own sessions.

`GET /api/notifications/devices/{device_id}/pending` returns due, unresolved
items younger than 24 hours. Each item carries its stable notification id and
only a fixed reason code, project name, and hash-route deep link as content.
Links use `#/projects/{project_id}/targets/{target}/{kind}/{item_id}`, with
each value URL-escaped; both episode reasons use `episode` as the link kind.
Bulletin mentions use the channel query link described in
[Bulletin channels](#bulletin-channels).
The same pattern names project references with the kinds `artifact`, `node`,
and `paper` (item id `introduction`); `target` is the node's source target.
Graph item age starts at its accepted Patch, and terminal episode age starts
at its latest lifecycle update, so downtime does not renew the 24-hour TTL.
`POST /api/notifications/devices/{device_id}/items/{notification_id}` accepts
`status: posted` or `status: failed`. Posted means the native notification
center accepted the request, not proof of display. A failed attempt is retried
with backoff; an unacknowledged attempt remains eligible for retry with the same
id. Success for one device does not settle another device's row.
Each episode item retains its observed health and blocked reason internally;
delivery drops it if the current pair differs, even within the same toggle.

Delivery pulls and acknowledgments authenticate without refreshing the team's
idle session expiry or cookie lifetime. Shell polling must call these endpoints
directly, without an identity-refresh request before each poll.

The Mac shell posts through a small native adapter over
`UNUserNotificationCenter` (`web/src-tauri/src/notifications.m`), not
`tauri-plugin-notification`, which on desktop drops the notification id and
click data and discards delivery errors. The stable notification id is the
request identifier, so a repeat replaces rather than stacks; the deep link rides
in `userInfo`, and a click routes the window to it, including a click that
launches the app. On its first pull after launch, more than three waiting items
post as one summary that opens the app; a summary macOS accepts acknowledges
every item it covers, and a rejected one leaves them for retry.

`GET` and `PATCH /api/projects/{project_id}/notifications` read and update the
calling member's toggles: `proposal`, `decision`, `blocker`,
`episode_needs_action`, `episode_finished`, `consolidation`, and
`bulletin_mention`. All default on except `episode_finished`. These routes require project membership. Project Settings
shows them as a **Notifications** card after Machines, which also says the
project name appears on the lock screen.

The Web app resolves a notification link once, at load or on a hash change,
into the item's ordinary route: a Proposal, Decision, or Blocker opens the Inbox.
A Decision or Blocker then opens its node detail as soon as the graph holds it,
resolved or not. A pending Proposal scrolls to its card; a resolved one shows
its outcome as a notice. An episode opens its exact run when it is still
listed, else Runs. The link is read
at module load, so a team sign-in in between still continues to it.
A reference link opens the current item: an artifact opens its viewer, a node
opens on its source target, and the paper opens under Artifacts. Copy reference
writes the full URL. The chat composer turns a same-project reference link into
a reference chip.

## Phone push delivery

Phones use standard Web Push with VAPID. `GET /api/notifications/web-push/key`
returns the space's application server key; the signing key lives in SQLite,
so it is part of every backup, and restore keeps it while detaching every
device. A missing key is never silently replaced while phones still depend on
it. No Apple or Google account is involved, and the server needs no public
address. Encryption and signing use `cryptography` sent through the existing
`httpx`, not `pywebpush`, which would add `requests` and `aiohttp`.

`POST /api/notifications/devices/web-push` takes the browser's
`PushSubscription.toJSON()` shape and binds it to the calling team session,
replacing that session's previous phone. A personal space refuses it on the
owner API. The endpoint must be `https` on the default port at an allowlisted
push service, and every resolved address must be public; anything else is
refused before a connection. A resolver outage is not a refusal: registration
answers 503 and a queued send retries. The request's HTTPS `Origin` becomes the VAPID
subject. Each send validates again, connects to the checked address with TLS
verified against the push service's name, and never follows a redirect.

The sender loop delivers due phone items with the same qualification, TTL, and
backoff as the desktop pull, requalifying each item and rereading the
subscription just before its send. The stable notification id is the `Topic`. A 2xx
reply means the push service accepted the message; display is best effort. A
404 or 410 deletes the device, 429 and 5xx retry (honoring `Retry-After`), and
any other reply drops the item and marks the device Delivery failed.
`GET /api/notifications/devices` lists the caller's devices.
`POST /api/notifications/devices/{device_id}/test` sends one test push now and
sets the device status from its outcome. `DELETE
/api/notifications/devices/{device_id}` removes the caller's own device of
either kind. The encrypted payload holds the notification id, reason code,
project name, and, for a device that may open items, the deep link.

A personal space pairs notify-only phones instead. `POST
/api/notifications/phone-pairings` issues one code in the team device-code
format, with the same expiry and lockout, and withdraws any earlier live code.
While a code is live the backend serves a separate loopback listener on port
8422; it stops once no code is live. The person routes one HTTPS name to it,
for example `tailscale serve`. That listener never reaches the owner API: it
serves only the pairing page, the web-app manifest, the service worker, its
icons, the public key, and `POST /api/register`, which redeems the code and
registers the subscription in one transaction. Redemption creates no session
and returns nothing that grants read access; the phone's payload has no deep
link. Restore voids every code along with every device.

## API composition and mutation boundary

One FastAPI backend serves JSON and the built React/Vite app; Tauri starts or
reuses it. `api/app.py` owns composition, dispatch, startup recovery, and watcher
runtime. Routes resolve identity/membership, validate and stage intent, then
call the concrete owner. Canonical mutation follows
[design invariants](../design.md#cross-cutting-invariants).

Graph mutation responses atomically project target, head, graph, Experiment
controls, guidance validity, transition/ruleset identity, primary question,
counts, and causal/attention inputs from one final state. Previews are explicitly
noncanonical and name their base head/ruleset. Backend-only derivations belong in
these projections; UI inputs such as the trust lens stay client-side.
`web/src/core/types.ts` restates shapes, using opaque lifecycle types to prevent
raw-status branching.

Attention publishes exact Proposal, Decision, and asserted-open-Blocker ids,
plus `stale_proposal_ids`, the pending Proposals the Sync gate would withdraw,
and its subset `refreshable_proposal_ids` that ordinary Work can re-file, and
`stale_edge_ids`, the
[stale assessments](graph-history-and-transitions.md#stale-assessments).
A display cache written before one of these staleness fields existed takes all
three from the graph it carries.
Proposal action lines include removed incident relations and `before`/`text`
field values. Counts use that membership. Inbox, Overview, and Runs map the
projection, never reapply predicates. Backend previews carry candidate membership;
rule-inert drafts retain the current membership. Missing/malformed membership,
action sets, graph member types, or inconsistent counts invalidate a snapshot.

Experiment controls publish budgets, reasons, history, health, recommendation,
Runs section, liveness, Stop state, exact Start/Resume/Retry/provider-switch/report
offers, and human-closed node state. Runtime, parent, tasks, budget, and report
come from one SQLite snapshot. Task polling cannot override it. Recovery binds
the named operation and disappears if its row is unavailable. A candidate graph
must be synced before Start; a rule-inert prose draft needs no such fence.
Snapshots also publish canonical mutation availability; watchers publish Check now.

Reads are start-order fenced per project/target even at equal revision. New reads,
settings saves, and Sync fence older graph/control responses. Every branch route
proves project and episode ownership; branch id alone grants no lookup. Both
Experiment and Auto-research branches support lists, targets, indexes, Research,
Inbox, Chats, and merge eligibility.

Graph, snapshot, history, Sync/preview, and ordinary chat/task routes take optional
`branch_id`; omission selects main, except project-wide task/watcher lists retain
their all-target default. Snapshots include `graph_target`, exact `graph_head`,
and `graph_changes` (null on main). A legacy cache missing the transition id keeps
the client's known id at that revision. `graph/changes?branch_id=...` returns one
coherent base-to-head semantic delta, neighbors, values, and Patch/task provenance.

Durable display caches are main-only; branch cached-snapshot reads explicitly
miss. Authoritative branch reads validate without repairing/publishing graph
outputs and fail if refresh is unavailable. Target resources never replace each
other. An active merge makes graph mutation unavailable, rejects Sync/new writers,
and fences new Discuss turns on the reserved binding through cleanup.

## Human question API and cards

Project members read shared questions; private-thread questions are visible only
to their owner. Reads use
`GET /api/projects/{project_id}/chats/{chat_id}/questions` and
`GET /api/projects/{project_id}/episodes/{episode_id}/questions`. Episode lists
include predecessor questions without changing their origin. Each question
publishes its id, owner and asking operation, origin capability, text, choices,
multiple-selection flag, state, answer, chosen choices, human resolver and time,
creation time, `withdrawn_readonly`, the server's `can_answer` offer,
`answer_continues`, and `agent_waiting`. `answer_continues` is `turns` on the last
open card of an episode that ended other than by Stop, `ending` while that
episode still settles, and otherwise null. The latter is true for an open, non-withdrawn question whose last
pending ask round arrived less than 10 seconds ago, using the server's monotonic
clock. This process-local freshness tolerates the remote mailbox polling cadence.
Open questions whose asking turn has settled, and orchestrator questions, project
as `parked`.

`POST /api/projects/{project_id}/questions/{question_id}/answer` accepts
`answer` text and `choices`, plus `invocation_ceiling` and a `request_id` that a
`turns` card requires: the answer then creates the episode's continuation, as
**Add turns** does. Answering a `turns` card without both returns 409
`question_continuation_required`. If the continuation cannot start, the answer
reopens and the error is returned. A retry once a continuation began after the
answer replays the answer and adds no turns. A card whose
episode still settles refuses with `episode_still_ending`. The store validates
the answer and records the authenticated human. An exact retry is idempotent; a conflicting resolution or
a withdrawn question returns 409. The endpoint enforces the store's resolution
rules independently of the displayed offer. Both a new answer and an exact retry
call the idempotent chat/Experiment answer reconciler, or record orchestrator
answer mail and invoke ordinary mail delivery. Immediate delivery failures are
logged with the question id and exception; the committed answer remains a
successful response. An exact retry or durable reconciliation retries delivery.
Answer input cannot change the original capability, scope, graph target, or mode.
The strict request rejects additional fields.
`POST .../questions/{question_id}/dismiss` accepts an empty object, resolves the
card only, and never wakes an owner.

Approval questions add `card: {kind, action, pinned, pinned_sha256, expires_at,
state}`, where state is pending, running or expired. Ordinary answer and dismiss
return `409 approval_card` for these questions. The owner resolves through
`POST /api/approval-cards/{question_id}` with
`{decision: "approve" | "decline", pinned_sha256}`. The response is
`{outcome: "approved" | "declined" | "stale" | "failed" | "expired", result?, reason?}`;
a second tap returns `409 card_taken`. The
[authority spec](authority-and-proposals.md#private-thread-approval-cards) owns
pinning, admission and delivery.

Node and project chats show resolved cards read-only in transcript order and
open question cards at the end of the transcript, scrolling with it. The
composer retains its steering behavior.
A single choice submits immediately; multiple choices use toggles and explicit
submission. Free text remains available with either choice format. A parked
chat answer with no running turn names Discuss or Work continuation on its submit
control, matching the asking turn's capability.
Experiment and Auto-research detail show the same cards; a `turns` card adds an
**Add turns** field beside Answer, and cards a Stop withdrew retain their history
with an **Episode ended** state. Chat and episode views select
questions from the shared project resource, refreshed by its watermark,
including an episode's status or ending change, and invalidated by question
commands. Fresh pending rounds show a cobalt **Agent is waiting**
pill; otherwise the open card shows **Needs you**.
Question notifications open the corresponding chat or expanded episode view.

## Episode merge API

`POST /api/projects/{project_id}/episodes/{episode_id}/merge` resolves the
isolation owner from any member. It also accepts a code-only owner. The optional
body names `target_branch`, `history_mode` (`merge` or `squash`),
`remove_worktree`, `delete_code_branch`, `archive_graph_branch`, and
`keep_branch_open`. Cleanup defaults to all three steps. Squash disallows Keep
branch open. Deleting the code branch requires worktree removal and delivery.
Merge stays human-dispatched. A clean merge runs without a provider turn.
Episode responses include the server-owned `can_merge` offer (see
[Runs projection](auto-research-and-branch-merge.md#runs-projection)) and the
owner's nullable `isolation_state`. The state carries
the reservation, attempt phase, recorded cleanup steps and errors, delivered
code commit and target, squash commit, and graph archive flag. Graph receipts
remain the independent graph delivery record.

`GET /api/projects/{project_id}/episodes/{episode_id}/merge-preview` accepts an
optional `target_branch`. It recomputes `MergePreview`; no preview is stored.
The response contains `delivered_baseline`, a `graph` object with the counts
`direct`, `conflicts`, and `reviews` (one per connected review group) and per-path
`paths`, an optional `code` status, and `needs_agent`. Each path names its entity, id, and
field path (`$` for whole-entity presence), its base, branch, and destination
(`main`) values, and the plan's `delivered`, `conflict`, `needs_agent`, and
`needs_proposal`, all from the same merge plan. `needs_agent` is true when the
plan has a conflict or a review group, or code conflicts. Code reports its
repository alias, source and target branches, commits ahead, leftovers, and
conflict files. Its status is `clean`, `conflict`, `already_merged`,
`target_dirty`, or `target_missing`.

`POST /api/projects/{project_id}/episodes/{episode_id}/cleanup` accepts the
three cleanup choices and `confirm_discard`. Unmerged removal or graph archive
requires that confirmation. Each step is recorded and retryable. Graph archive
only hides the branch's ended episodes from the default episode list; a live
episode stays listed so its Stop remains reachable. The list query
`include_archived_branches=true` includes it again. Explicit episode and branch
reads remain available, and Patch history is unchanged. The default episode
list is the 50 most recent plus every live episode, retained archives, and the
current member of every Auto-research chain, so each orchestrator card in Agents
has its episode.

Merge and cleanup pause on the
[job gate](machines-and-compute.md#episode-merge-and-removal-gate): a live or
unobservable job returns 409 with a `jobs` list, and resending with
`confirm_unfinished_jobs: true` proceeds.

## Compute setup and job APIs

Project machine entries carry `alias`, `host`, `os_account`, `removable` (no
repository or agent profile uses it), the space card's `compute` block, and live
`compute_probes`, one `ComputeBackendProbe` or null per route. Both are read
from the card and storage even when the graph snapshot is cached. Project
Settings saves no machine settings.
`DELETE /api/projects/{project_id}/machines/{machine_alias}` uses project write
admission, refuses a machine a repository or agent profile uses with 422, and
returns the project snapshot; the space card stays.
`POST /api/projects/{project_id}/machines/{machine_alias}/compute/check` uses
project write admission, re-probes every route that machine offers, and
returns both route slots.
`GET /api/projects/{project_id}/machines/{machine_alias}/browser` returns the
browser readiness of that machine's execution account; the composer reads it
while a chat's Browser toggle is on and warns when it is not ready.

`GET /api/projects/{project_id}/watchers` supplies the external job rows for both
scheduler and helper work. Every external row includes its required shell check,
log path and cwd, optional cancel command, check state/diagnostic, and cancellation
requester/time/diagnostic. The backend exports `can_cancel`; the browser does not
derive it from watcher status. There is no separate compute-job list request.
Without `branch_id` the list carries every target's rows. With `branch_id` it
carries that target's rows, or every target's with `all_targets=true`, which a
branch view's Runs uses for sibling loops. Either way `can_stop_watching` is
offered only on rows of the displayed target.

`POST /api/projects/{project_id}/watchers/{watcher_id}/cancel` requires project
write admission and an attributed human. A missing or foreign-project watcher
returns 404. An already-observed completion or accepted cancellation request
returns the row unchanged; no cancel command returns 409. A fresh precheck may
establish completion without attributing it to Cancel, or report why the action
could not run. Otherwise the route claims and executes the saved command once.
Its result is an action receipt, not a separate cancelled watcher state. Errors
are returned on the watcher and allow an explicit retry.

The machine card's Compute fold holds **Slurm**, helper `jobs_root`, and the
access hint; RCP exposes no resource-argument inputs. Each saves on its own.
Runs offers **Check again** through
`POST .../machines/{machine_alias}/compute/check`. Each stored probe supplies
its own label, tone, and diagnostic.

Chat and Experiment use the existing watcher refreshes to show one external job
row. It displays the log path, observation state, check diagnostic, and Cancel
history. Human Cancel follows the backend's capability even for a stopped
watcher, independent of graph read-only mode; the API enforces project write
admission. The immediate action response updates its row without adding a
second job-list polling lifecycle.

## Atomic client project snapshots

The Web client holds one atomic `graph` resource per project and ref. Graph,
control, guidance, attention, and head are accepted together, including control
changes at an equal graph revision. History and the parameterized transition
manifest use the same graph watermark. The client never overlays a new graph on
an old control map and never implements transition rules. Human drafts and
preview coordination remain local per ref and reconcile against accepted data.
Chats, watchers, episode indexes and entity timelines use their own resources;
transcripts are keyed by their owning ref. Merge previews remain demand-only,
keyed by the episode, target, and preview inputs.

Human edits that the backend manifest proves rule-inert may update a local draft
immediately. A possible trigger, absent manifest, or stale tag previews through
the backend. A preview conflict retains the invalid edit and last valid draft
separately. Sync revalidates the complete batch against the live canonical target
and commits once or not at all.

Human Sync accepts built-in and active custom nodes through the existing
`custom_nodes` collection, plus `added_edges` and `removed_edge_ids`. Preview and
commit share the same backend owner and exact base revision. Edge replacements
remove the old edge before creating its replacement in the same transition;
layers are resolved by the backend. The membership-protected `graph-edit-options`
endpoint publishes built-in relation names with their endpoint types and
same-type rule, Evidence-assessment requirements and node ID prefixes from
backend rules without replaying project history. The
client reads custom relations from the displayed ontology; backend preview completes
new node defaults and resolves edge layers. A draft with edge
changes retains its original edge-edit revision across refresh/reload; it cannot
silently rebase connection edits onto a newer graph.
These controls remain human-only and add no WebMCP graph-writing tool.

Resolved/superseded Blockers remain canonical but are omitted from active
Research-flow and attention projections. Stale Experiment summaries and next
actions are labelled historical and never rendered as current guidance.

## Revision observation and drafts

One store per backend/member holds resources keyed by project, resource, and
applicable ref/entity/query. Feature-owned profiles supply fetch and reconciliation;
components select from that store. Tabs retain UI state only: selections, panels,
drafts, scroll, and editor generations. Each open tab subscribes to its current
ref; other visited refs stay dormant until reopened. There is no count cap or
unvisited-ref warming. Closing a tab releases its resources.

One completion-scheduled watermark request sends the complete open set. Active
projects are due every second, hidden projects every third beat, and expired
deadlines immediately. Only one request runs at a time, with a five-second
browser timeout and failure backoff of 3, 6, 12, then 30 seconds. Hidden pages
pause; becoming visible coalesces validation. Payload reads use three slots,
at most one for background work, with active priority and aging to prevent
starvation. Activation paints cached data and promotes reads without awaiting SSH.

Every read captures its immutable key, lifecycle generation, and start sequence;
only a matching read commits. Observed and committed tokens are separate: failed
or superseded reads keep their refresh debt. Epoch changes fence all resources
and clear both tokens. Backend/member changes reset the store. Unavailable
projects lose route, tab, resources, and viewport. Contact failures preserve data
with an accessible stale marker; remote-sync health has its own marker.

All open tabs watch project-wide task status; only the active tab watches progress
and displayed task details. Other resources have their own tokens/profiles. Task
completion does not trigger a reload bundle. Paper is an ordinary profile reading
the live tree and writing sessions. The project snapshot carries only its
publication badge; source text and durable acknowledgements belong to the
[live editor](paper-artifacts-and-result-views.md#shared-paper). Paper links use
`paper_file=<stable file id>` to select a file, including in an already-open editor.

Commands fence affected reads and install operation-id pending overlays;
conflicting commands serialize. Success replaces the overlay with the response;
a definite failure removes only that overlay and offers retry without losing
newer state or typed text. Ambiguous chat sends retain their card and idempotency
key for Resend; ambiguous task actions reread the task and retain the key, rather
than claim failure. Reads cannot regress behind accepted mutation responses.

**Why:** One resource owner and one acceptance rule prevent active/hidden tabs,
restored UI state, and late target reads from overwriting each other. Status-only
subscriptions keep progress from causing hidden-tab traffic.

Draft previews are demand-only and input-keyed. PTY sockets, artifact execution
and viewer live data, voice watches, and the landing Runs ledger stay outside
this refresh loop. Explicit probes and mutations publish or invalidate resources.
A staged node stays committable if its canonical revision did not move; otherwise
it is behind and excluded from Sync until edited or an incoming field is
reversibly swapped in. Paper uses its whole-document equivalent. Display caches
are never canonical input. When a graph-capable turn closes, RCP rebuilds the main
display cache from canonical state, replacing a missing or invalid one too.

## Project index and identity

The index leads with project cards, then a space Runs ledger across visible
projects. Backend projections select Experiment parents and Auto-research chains,
health/section, project identity, and exact routes. Needs Action/In progress mix
modes newest first; Completed folds by mode and keeps seven days without deleting
records. Exact Auto-research parents outside bounded lists are fetched separately.

Project cards carry `group_id`, and `GET /api/project-groups` lists the
[groups](projects-spaces-and-operations.md#project-groups) the caller sees, so
empty groups appear too. The index shows each group as one box-set tile, the
size of a cover, with a strip of its projects' cover colours. Opening a tile
shows that group's shelf, whose breadcrumb leads back to the space; the open
group lives in the URL hash. Dragging a cover onto a tile moves the project into
that group, and dropping it on the breadcrumb takes it out. The cover menu's
**Move to group** does the same without dragging, for phones. `POST`/`PATCH`/
`DELETE /api/project-groups[/{id}]` create, rename, and delete (409 while not
empty); `PUT /api/projects/{id}/group` moves one project.

The header identity panel edits display name and shows a copyable user id.
An unnamed personal owner's Sign in names their durable identity; authentication
is separate. Team members manage their credential through the team boundary.
Pending invitations appear here. Membership-hidden projects never appear locked;
access loss closes their tabs and returns to index.

Team Devices lists the member's unexpired sessions, refreshing on open, revoke,
or explicit Refresh. Current device uses Logout, never Revoke. `GET
/api/team/sessions` returns `session_id`, label, creation/last-seen/expiry times,
`is_current`, and backend `can_revoke`. Public ids are random UUIDs unrelated to
secrets; listing does not renew other sessions. `POST .../sessions/{id}/revoke`
returns `{ok: true}`; unknown/expired/foreign ids share 404, current returns 409.
These routes are 404 in personal spaces and retain auth/origin checks.

Connect a device POSTs `/api/team/devices/pairings`. One live code per member
replaces the previous unused one. Codes have ten unambiguous characters, display
as `ABCD-EFGHJK`, expire in ten minutes, are single use, lock after five failures,
and store only secret hashes. Ending the issuing session revokes its code.
The visible card polls `.../pairings/{id}` for waiting/consumed/expired/revoked/locked;
consumed refreshes Devices.

Operator `access_url` in `/etc/rcp/team.toml` (source: `RCP_TEAM_ACCESS_URL`) is a
read-only HTTPS origin. GET `/api/team/space` returns it; PATCH changes only name;
doctor reports `team_access_url`. Configured codes include
`connect_url = <access_url>/#pair=<code>` for QR; otherwise the card names setup.
See [device pairing](../device-pairing.md) for the operator/member journey.

Public `POST /api/team/devices/pair` accepts Device code and required Name this
device, creates an ordinary labelled session, and sets the exchange cookie.
It neither receives nor returns the permanent token or needs shell negotiation.
Public enrollment/exchange body bounds apply. Failure codes are 401 invalid,
409 used, 410 expired, 429 locked. Login also offers team-token sign-in.
Session exchange accepts optional untrusted `label` <=80 characters, defaulting
to Unnamed device without fingerprint inference. There is no rename route.

The desktop retains one session per saved connection. Keychain stores only its
cookie secret (not the oversized Set-Cookie line), alongside the member token.
Native launch, Reconnect, and requests verify identity; only 401 triggers exchange.
Forgetting discards the local copy; the server session expires or is revoked.

Personal Devices instead lists notification targets (This Mac/notify-only phones)
with Remove and Connect a phone. Team rows show notification on/off/Delivery failed;
only the current device offers Turn on/off/Test. Native uses macOS notifications,
browsers Web Push, with permission requested only on tap. Unsupported browsers
get Home Screen guidance. Visits re-register live subscriptions and remove server
registrations whose browser subscription is gone.

## Confirmed team desktop target

The macOS desktop groups personal projects and saved team connections on its
local index. Each connection holds nonsecret routing, expected `space_id`,
compatibility metadata, and bounded cached cards. Permanent tokens stay in the OS
credential store, never URLs, page storage, registry JSON, logs, or command output.
**Add team space** opens the SSH tunnel, checks identity, then enrolls with a code
and name or exchanges an existing token. Native storage captures issued tokens;
the secret field and IPC request are cleared after that operation.

The saved rows are listed when the app starts and kept across index visits, so the
index paints them on its first frame. A row being checked keeps its **Open team
space** action in place, inert, and a row already available stays available while
it is checked again.

Before enrollment, token exchange, project-card reads, or cookie installation,
the shell negotiates the highest overlap of inclusive integer protocol ranges
and requires the exact echoed integer. Missing/mismatched negotiation or changed
`space_id` refuses the connection; diagnostics name both source commits and a
compatible release/update path. Semantic versions and commit order are not the
protocol. Personal work remains usable; unavailable team cards remain labelled.

Desktop supports `[3, 7]`, server `[1, 7]`. Protocol 1 forbids team deletion;
2 permits deletion only with negotiated support and the exact confirmation;
3 adds optional reviewed `source_commit` and `rcp-transfer-v2` Git archives,
while accepting omitted commits/v1 archives; 4 adds optional
`record_schema_version=2` for episode archives; 5 also allows 3 (an archived
graph branch) and 4 (branch names); 6 also allows 5 (a deleted episode
report); 7 also allows 6 (graph branch records). The desktop refuses preparation,
before any target mutation, when the source needs a record version the
negotiated protocol lacks, so a new record
version needs a new protocol. Archive-free transfers retain prior bytes; incompatible peers refuse preparation before source release.
Breaking contracts get a new immutable version; retirement is explicit.
The range governs native bootstrap and relay, not server operations, provider
work, or the server-served Web/API. Negotiation is live, never durable authority.
Registry version 3 migrates version 2 without changing identity, routing, cards,
operator metadata, or Keychain references. Unknown versions/fields fail closed,
except cached cards and extensible sign-in responses. Cards keep id, name, and
attention count, ignore other fields, and are dropped to fit the size bound;
server display text cannot fail a connection.

Each space serves its own index. Leaving a project returns there; **Exit team
space** on the team index reloads the personal backend. Only the personal index
shows multiple spaces. HTML uses `Cache-Control: no-cache` with ETag validation
so reload/entry sees updates; hashed assets retain normal caching.

The shell runs `/usr/bin/ssh` directly with argv, batch authentication, no remote
command/TTY, exit-on-forward-failure, bounded timers, keepalives, no control
sharing/backgrounding, and one loopback forward to the configured server port.
Existing SSH config/agent supplies host and key policy; RCP never prompts for an
SSH password. A dual-stack loopback TLS listener forwards only to that private
SSH listener. Healthy tunnels are reused only while origin, target, and port
match; failed/changed tunnels back off before replacement. A team page starts
that reconnect only when fetch itself fails to reach the server; a timed-out or
cancelled request is not an outage.

Each saved connection has a stable origin:
`https://rcp-<connection-id-without-hyphens>.rcp.localhost:<local-port>`.
The main window admits the personal backend, applicable Vite origin, and exact
validated saved HTTPS origins. A bounded Tauri hostname capability is not
navigation authority. The backend recognizes only the corresponding generated
HTTPS Origin over its private HTTP tunnel boundary.

One desktop certificate/private-key pair lives in an authenticated encrypted
0600 file; only its 32-byte AES-256-GCM sealing key is in Keychain. Apple's signed
Keychain tool carries the key through pipes, never argv, using its stable tool
ACL. The version-4 record holds expiration; its 365-day non-CA leaf covers
`*.rcp.localhost`, includes TLS server/key usage, and rotates atomically in its
last seven days. Valid older records are reissued; malformed, unauthenticated,
unknown-version, or partial file/key pairs fail startup. New identity is generated
only when both halves are absent. Exact old `.localhost` registry origins migrate
without replacing connection identity or credentials.

WKWebView trusts only the pinned leaf after hostname, validity, and server-use
checks. ATS has a narrow exception for `rcp.localhost` and its direct subdomains;
there is no system trust installation or HTTP app navigation. Each TLS listener
rejects another team's SNI before opening upstream. Team tokens have a versioned
credential-store service and a distinct account per connection.

**Why:** Cookies ignore ports, and WKWebView requires HTTPS for the Secure team
cookie. Distinct hosts isolate sessions; `*.rcp.localhost` gives a normal one-label
wildcard. Exact origin, certificate pin, and per-listener SNI checks serve separate
purposes. The tool ACL survives unsigned rebuilds but gives no same-account read
isolation, for source or prebuilt apps; app-bound credentials need a stable
signing identity.

Tunnel admission closes before removal, Quit, or Update drains children. Failed
lifecycle work reopens admission only on return to service; unconfirmed cleanup
retains ownership for retry/diagnostics. Personal pages can connect any saved row;
a team page can reconnect only its own. Native Reconnect is single-flight with
capped backoff, then rechecks identity without restarting the remote service.
A recovered tunnel, Dock reopen, or notification revalidates the shown project
through one immediate watermark beat; the page stays interactive, and only a ref
switch holds it inert.

Backend contact failure keeps the rendered page beneath a blocking overlay,
preserving panels, scroll, and text. Same version/instance/data-directory/space
recovery reconciles normally; changed identity requires explicit reconnect and
a fresh project open. A changed watermark epoch reverifies identity, so a restart
offers Reconnect before the next write. A write the instance fence refuses
(409 `backend_instance_replaced`) admits nothing: the composer keeps its text,
and Reconnect reopens the chat the route names. Ordinary browsers can use a
reachable team UI but cannot own tunnels, credentials, multi-space routing, or
native commands, and do not offer Add team space.

A page leaving through a space switch does not report the reads the switch
cancels. The switch commands mark the exit before navigating. If the page is still
shown when the exit window closes, the held failures surface as usual and count as
transport failures.

**Why:** WebKit rejects a leaving page's in-flight reads as network failures while
that page is still on screen. Reporting them flashed error notices on every space
switch and started tunnel recovery for a tunnel that was fine.

One project wizard offers **Use an existing checkout personally**, **Create a
shared team project**, and **Move an existing personal project to a team**.
Project cards and Settings can preselect Move. Each backend publishes its own
eligibility, required fields, preselection, and pinned identity. Move requires
personal permission, target admission, and explicit native relay capability;
space kind or saved connections alone confer no authority.

Personal setup uses path preflight/finalization. Team creation uses durable
provisioning requests and central paths. Repositories take optional GitHub
`source` (blank/null means server-only) and `count_as_project_truth` (default true).
Responses expose `source_kind`, nullable repository/GitHub identity, and truth
choice. `POST /api/projects/{id}/repository-requests` accepts `add_repository`
(alias, exactly one of a GitHub `source` or an existing folder `path`, truth
choice defaulting to false) or `connect_repository` (alias, source). An add always
lands on the project's base machine, so it names none; Settings offers no empty
server-only add. A folder must not overlap another project repository; Connect
refuses an adopted folder and its Settings row has no `can_connect`. Existing project membership gates reads and requester confirmation.
Add commits membership/truth through a human transition bound to the reviewed
manifest/head; Connect preserves the manifest.

All requests use the same setup/review controls and
`rcp server project provision <request-id>`. Settings submission never confirms.
Team direct `/api/projects` registration and `/api/project-setup/{preflight,ssh-paths,create}`
are refused before parsing or filesystem access. Only the validated provisioning
finalizer reaches registration. Server-only review warns code is not backed up;
empty setup announces its first main commit and optional GitHub push. RCP collects
no GitHub user authentication.

Transfer links requests in both backends. GitHub repositories retain their origin;
server-only repositories require the reviewed commit and Git bundle. Native parsing
preserves nullable sources. Target URLs are not selected during transfer.

Health details publish package `version`, installed-process `running_commit`,
nullable package `build` and hexadecimal `commit`, and `schema_ledger_head`.
Their `project_creation` projection names all three intents and exact actions.
Provisioning projects labels/tones, diagnostics, paths, readiness counts,
`can_run_setup`, `can_review`, `can_cancel`, safe operator argv, and review binding.
Ready provider checks include executable/version/runtime/account/time proof,
never credentials or provider-home paths. The Web renders these answers; native
subprocess success alone never proves completion.

Member connection and operator route are separate. A native operator route is
explicit direct service-account SSH or a named route using noninteractive sudo.
The Web derives the route form from the login. **Run setup now** requires a
read-only probe for the fixed provision command. Native execution takes only a
validated request id, bounds/screens structured events, and verifies durable
readback from the expected team space. Interactive SSH/sudo instead opens or
shows that fixed command in Terminal; browsers offer its copyable operator form.
No arbitrary server-returned command is executed.

CLI progress is presentation only; the CLI reports state through the backend's
private control channel. Final human review alone creates/re-homes a project.
Transfer confirms target admission before source release, each with its own actor;
a partial confirmation remains resumable. Cold operations restore the exact saved
tunnel/session. Nonzero relay exit or incomplete proof/cleanup remains a retryable
error, never silent manual fallback. Operator events carry responsibility, target,
safe commands/actions, values, success check, and resume command shared with CLI.

## Project tabs

The project shell keeps a session-scoped dock beside the index control. Opening
a project appends and activates one tab; reopening activates without duplicating
or reordering. Inactive tabs shrink within the capped dock while the active one
retains more width. Tabs cannot be reordered.

Each tab retains bounded in-session view, panel, scroll, selection, draft, and
exact Runs-route state. Every project view writes its route, and leaving a tab
remembers the exact route it left, so returning lands on that page even when the
tab's snapshot is gone (for example after a backend restart). Within a project,
each view tab reopens the sub-view the viewer last had open there (Research its
graph, Artifacts its Paper, Agents the chat it was left on while that chat still
exists, else its board), remembered per project in browser storage. Activating a tab is
cache-only and never blocks on remote I/O. Closing a tab changes no canonical state, task, draft, conversation, or
project data. Closing the active tab selects the right neighbor, otherwise the
left, and the last close returns to the index.

Open tabs survive hiding/reopening the same desktop window but reset on full
page reload or app quit. An inactive tab is not kept mounted merely because it
is open. A browser reload keeps the project route, so a phone browser that
reloads a discarded tab returns to the same project; a desktop reload or
relaunch starts on the index with an empty dock. A successful team sign-in
keeps the route it interrupted. A restored route never skips identity
admission: the project open still checks access. Retained project state and all
project tab snapshots belong to the backend and member that loaded them. A
change of backend or actor identity discards them before restoring the route,
including when another member signs in on the same backend.

An explicit Runs route is authoritative over cached selection, including a route
with an absent or malformed branch identifier. Invalid branch identity resolves
to no selected Experiment rather than restoring a cached main-target selection.

## Browser-agent WebMCP surface

When `document.modelContext.registerTool` exists, the verified project index
exposes listing/navigation; a loaded project exposes overview, nodes, provider
sign-in status, views, artifacts/reports, conversations/Send, Experiment controls,
Auto-research authorization, and graceful Stop. Login, setup, loading, invalid
identity, and reconnect retire tools. Overview supplies role agents and stoppable
Auto-research ids. Actions exist only while possible or while an accepted call
returns. Stable proxies update without aborting calls; navigation returns before
its old surface retires.

`web/src/voice/toolCatalog.ts` owns the host-independent catalog, schemas, local
`confirm(args)`, Responses serialization, and page-state `resolve(name)` refusal.
Confirmation applies to Work Send, Experiment Start, Auto-research authorization,
and terminal commands. It is local to voice, not WebMCP host confirmation. The two
terminal tools are voice-only because WebMCP has no RCP confirmation card.

Calls revalidate exact ids against the current page. Named reads return bounded
JSON, omit storage locators, and mark shortened node paths. Conversation listing
includes loaded ids and total; inspection/Send fetch the latest exact task outside
the list window to retain graph-target/session ownership. Branch chats resume only
on their target.

Project memory is agent-managed file data, with no lesson read in WebMCP and no
lesson CRUD API. Its ownership and migration live in the
[memory spec](graph-consolidation-and-lessons.md#memory).

Artifact listing combines task/episode windows (reporting their sizes) with the
Artifacts inventory. Duplicates gain `in_artifacts_panel`; panel-only records use
`saved:` ids. Filters use source identity; exact old task/episode ids are fetched
rather than declared missing. Opening checks availability and uses the existing
viewer. Desktop PDF opening uses the panel's system-viewer command; browsers and
download-only files instead say where the member can tap Download. Tools never
download. Artifact payloads and omissions follow
[artifact ownership](paper-artifacts-and-result-views.md#answer-and-artifacts-are-independent).

`rcp_get_playbook` returns the plain-language playbook in `webmcp/playbook.ts`;
voice sends the same text. `rcp_list_read_routes` and `rcp_read` share the broad
reader in `webmcp/reads.ts`. Its code-owned policy intersects backend OpenAPI for
discovery and enforcement. It admits open-project GETs except downloads,
redirect-only routes, and side-effecting reads: terminal/Experiment/digest/project
reconciliation, merge preview, worktree Git contact, machine browser probes, and
live artifact file snapshots. `refresh` and chat `inventory` flags are refused.
Repository reads reject credential path components. Requests use validated route
templates, remain same-origin/project, refuse redirects/SSE/binary, and cap bytes
and duration. Identity loss cancels them. Branch-aware routes use the displayed
branch; history requires a bounded revision window. Results are paged and marked
untrusted; voice envelopes also name source and arguments. These broad reads
retain backend paths, unlike named reads.

Send launches one asynchronous ordinary Discuss/Work turn with the composer's
admission, profile, skill, session, and execution owners, returning its task id.
Attachments, references, and compute choices remain visible-composer controls.
Start/Stop reuse readiness, Sync, budget, single-start, exact-episode, and graceful
Stop guards. Auto-research takes integer `invocation_ceiling >= 1`, optional
`starting_instruction`, and `code_worktree`, through the header's start/refusal
owner. Stop takes exact episode id, using Experiment Stop when `experiment_id`
is present or the Auto-research episode route otherwise.

`rcp_open_view({kind, id})` opens an exact node, conversation, run, artifact, or
project tab (Inbox included) within the current project/target. A run without an
Experiment board entry, or on another target, is refused. First-turn chats open from their task.
Run ids resolve through notification routing. Only `rcp_open_project` changes
project; opening a view never changes branch.

Calls use the member's authenticated page and existing owners. There are no tools
for Proposal judgment, Decision choice, graph edits/Sync, Keep/Undo, settings,
membership, or project creation/deletion. Voice gets no browser/page-driving tool:
it could press its own Confirm and send the screen to OpenAI.

## Voice agent

The voice button in a private agent thread opens one GPT-Live session bound to
that thread; a second click ends it. Audio goes between the page and OpenAI
over WebRTC; RCP never receives it. Closed threads return 404 on voice access.
The backend checks thread ownership and current project membership before
reading its transcript or private memory.

`POST /api/voice/sessions` takes `{sdp_offer, tools, playbook, thread_id}`.
The member's OpenAI connection supplies the live and delegation models. The
backend sends the bound thread's newest historical text as `session.input`,
within `VOICE_RESUME_MAX_MESSAGES` and the byte budget that guarantees the
8,192-token limit. Historical text is labelled as context, never fresh authority.
The session uses `store: false`, Responses delegation, serial tool calls and
non-strict function tools, with fixed instructions before the bounded playbook.
`store: false` is not a zero-retention promise. Private thread text and memory
reach the member's own OpenAI connection.

The response is `{sdp_answer, input_truncated, limits}`. No voice session record
is created. The record list, generation, save, delete and Resume routes are gone;
`resume_id` is refused. Existing records retain the 30-day expiration rule and
are pruned when voice starts. Missing voice credentials return
`voice_not_connected` (409); upstream failures return `voice_upstream_failed`
(502), without upstream diagnostics or credentials.

`POST /api/agent-threads/{thread_id}/voice-entries` accepts
`{entries: [{entry_id, speaker, text, at}]}` and returns `{appended}`. Speaker is
`member` or `agent`. Entries are bounded display-only transcript records with
`source: "voice"`, no task operation and the thread's private owner. Repeating
an entry id in the same thread never appends it twice. A member voice utterance
runs backend work only through a separate thread Send, under its normal admission.
The page tool `rcp_thread_send({text})` makes that Send under the existing tap
confirmation. A running turn that takes input is steered instead; otherwise
voice gets `thread_busy` and nothing is sent. `rcp_thread_memory()` reads the
memory route below.

`GET /api/agent-threads/{thread_id}/memory` returns the bound project's
`shared_index?`, `private_index?` and `truncated`. Each index is bounded by
`MEMORY_INDEX_MAX_BYTES`, read on the project base, and refuses symlinks. Only
that member's private index is readable. A Space thread has no bound project
and returns only `truncated: false`. Voice asks the backend agent to write memory.

Each voice write (a conversation Send, an Experiment Start, an Auto-research
authorization) carries a page-minted UUID4 request id. The page saves it in the
receipt before dispatch and sends it as the `Idempotency-Key` header. The
admission route records (project, member, key) with the admitted task or
episode in the same transaction as the admission; a repeat by the same member
returns the original result, and the key from another member or route is a
409. Rows are never pruned; a retried client request can reuse its key at any
later time. They go with their task or episode.
`GET /api/projects/{id}/client-requests/{key}` returns the admitted ids (an
Experiment start also names its episode), or 404
when nothing was admitted or the row is not the caller's. A recovered key becomes an accepted receipt with a watch; a 404, which may be an admission
still in flight, or an unanswered lookup leaves the receipt unknown. A
re-ask reuses the receipt's key, so a send still in flight is never admitted
twice.

`GET /api/voice/settings` returns `{live_model, delegation_model, confirm,
idle_minutes}` from the member's private settings file; a session uses the
saved `live_model` (default `gpt-live-1`) and `delegation_model`, and its
`idle_seconds` limit comes from `idle_minutes` (default 5, range 1 to 60, in
`limits.py`). `PUT` takes `confirm`, `idle_minutes`, or both, and keeps the
other field: the voice models change only through the checked connection update
below, and the card sends only the models the member changed.
`confirm` is `tap` (the default) or `none`; the panel's toggle sets it. In the Dictation and voice card, the
**Standby voice agent** section picks the connection it **Runs on** (Off, or an
OpenAI connection). Choosing a connection gives it the `voice` purpose, which
RCP checks against OpenAI before saving. The voice models are set on that
connection's card (see Dictation below). With a connection chosen, **Ends
after silence (minutes)** sets `idle_minutes`.

The page runs each delegated function call through the shared catalog's
`resolve`, as the member. It runs one call at a time, ignores a repeated
`call_id`, and refuses an identical repeat of a call whose outcome is unknown.
A call the page cannot run now, including a Send its provider is not ready
for, is refused before any card. The panel transcript shows one line per call
with its outcome.
In `tap` mode, a Work Send, Experiment Start, or Auto-research authorization
first shows a card that pins the project, graph target, arguments, budget, and
for a message its mode and provider profile. The session then speaks one fixed
line pointing at the card. Confirm rereads page state and runs nothing if a
pinned value changed; a decline or timeout returns "not confirmed".

Voice can also use a project terminal. `rcp_list_terminals` reads the open
project's terminal repositories, the machine each runs on, whether it can open
a terminal now, and the open sessions. `rcp_run_terminal_command` takes a
listed `repository_id` and one `command` line of 1 to 1000 characters with no
control characters; the tool adds the Enter. Every run shows a card, even when
the panel is set to **Run without confirming**. The card pins the repository,
the machine, and the exact command, and Confirm rereads the listing. Voice
never reuses a shell. Each run opens a fresh one with `require_new`, so a
confirmed line never joins a half-typed line or feeds a running program; when
the repository already has an open terminal, the run is refused before the
card, and the server's 409 covers one opened after that check. The run waits
for the new shell's prompt to settle, types the line once, and reads output
until the same prompt returns on its own line or 10 seconds pass (with no
prompt to recognize, until output is quiet for 1.5 seconds). A command that
finished closes its shell. One still running stays open in the Terminals tab,
and voice does not type into it again. The result strips terminal escape codes
and returns the last 4000 characters as untrusted content, with `finished`,
`still_running`, and truncation flags. A confirmed command has the member's
full terminal power.

The page owns the session's lifetime. It ends the session on End, the idle
limit, the hard cap, identity or team-session loss, a 401 or 403 on a read,
leaving the space, and the page freezing or going away. The idle and hard-cap
deadlines are absolute times checked every second and on every event, so an
event that arrives after a late tick ends the session instead of extending it.
Hiding the page also ends it, except in a desktop window that keeps running
while hidden: the main window disables WebKit background throttling, which
macOS 14 and later honor, and `desktop_keeps_voice_while_hidden` reports
whether this window does. Browsers, macOS 13, and an older shell end on hiding,
and the panel says why. It sends
`session.close`, waits a bounded time for `session.closed`, then closes the
peer. After End, it runs no further calls. While open, the session speaks
first only when a Work turn, Experiment, or Auto-research episode it started
finishes or needs the member, polling that record through its own project's
routes; the spoken text is a fixed template with no authored content. A finish
offers its result; `rcp_open_finished_result` opens it only on the member's
next reply, under the current project and graph guards, and the offer expires
after that reply or the next agent response. Nothing opens on its own.

## Private thread schedule API

The thread owner lists schedules with
`GET /api/agent-threads/{thread_id}/schedules`, returning `{schedules: [...]}`.
Each schedule exposes its id, cron or local one-shot time, timezone, note, state,
disabled reason, next fire and last fire. Cancel uses
`DELETE /api/agent-threads/{thread_id}/schedules/{schedule_id}` and returns 204.
`POST /api/agent-threads/{thread_id}/schedules/{schedule_id}/enable` re-enables a
disabled schedule and returns it; invalid state, an elapsed one-shot time or the
active-schedule cap returns 409. Other members receive 404. Creation is through
the member-admitted `schedule add` command; the
[scheduling spec](graph-consolidation-and-lessons.md#private-agent-schedules)
owns the grammar and wake lifecycle.

## Consolidation API

`GET /api/projects/{project_id}/consolidation` returns `can_write`, `schedule`,
consolidation `inbox` rows, `recent_nights`, and `evidence_flags`. The last array
contains only open and addressing flags. Each flag has `flag_id`, `target` (the
consolidation run's graph target), nullable `run_id` and `occurrence_date`,
`evidence_id`, `evidence_revision` (the target's revision when flagged), `kind` (`overclaim`, `underclaim`, `no_source`,
`unverified`, or `consolidation_added`), `finding`, nullable `source`, `state`
(`open`, `addressing`, or `dismissed`), nullable `chat_id`, and `created_at`.
Findings are non-blank single lines of at most 400 characters; sources are at
most 1000 characters and use an absolute host path or `<alias>/<relative path>`
with optional `:LINE`, or an `http(s)` URL.

Both Evidence flag mutations require project write admission and a patch-capable
identity:

- `POST .../consolidation/evidence-flags/{flag_id}/dismiss` returns
  `{"flag": EvidenceFlag}`; unknown flags return 404 and dismissed flags 409.
- `POST .../consolidation/evidence-flags/{flag_id}/address` takes
  `{"request_id": string}` and returns `{"flag": EvidenceFlag, "chat_id": string,
  "operation_id": string}`. It admits an ordinary node Work chat on the flag's target using
  the human node-chat path and default node-chat provider profile. The first human
  message is composed on the backend from flag context and fixed instructions;
  it adds no authority. The request id is recorded as the admitted turn's client
  request, so replaying it returns that turn's chat and operation even after a
  later attempt or the turn's own end settled the flag. Addressing an addressing
  flag returns its current chat and records the request against that turn. Neither starts another turn, whatever the target now
  holds. An open flag
  that keeps a `chat_id` was reopened by a failed turn, and a new request id
  starts another. Addressing an open flag whose Evidence is absent or retired on
  the target, a dismissed flag, or an `unverified` or `consolidation_added` notice
  returns 409; unknown flags return 404.
- `POST .../consolidation/evidence-flags/address` takes
  `{"request_id": string, "flag_ids": [string]}` with 1 to 10 unique ids and
  returns `{"flags": [EvidenceFlag], "chat_id": string, "operation_id": string}`.
  One id admits the same node Work chat as the single route; two or more admit
  one project Work chat on their target bound to every flag. Every flag must be
  open and share one target, so an addressing flag or mixed targets return 409 here. The request key is bound to the exact id
  set, and a replay returns that request's own turn.

The [consolidation spec](graph-consolidation-and-lessons.md#evidence-to-check)
owns fact-check behavior, flag lifecycle, and the Evidence to check Inbox rows.

`POST .../proposals/{proposal_id}/refresh` (empty body; the id may contain
slashes) returns `{"chat_id": string, "operation_id": string}`. It admits a
light node Work chat on main on the Proposal's subject node, with a backend
message holding the Proposal, its moved dependencies, and its stale cause edges.
Its request key is
derived from the Proposal, so any repeat by any member returns the first chat.
Once that chat exists, every repeat returns it, whatever the Proposal's state.
Before it does, a Proposal that is not pending, not stale, a form ordinary
Work cannot file, or names no current node returns 409 with code `proposal_not_pending`,
`proposal_not_stale`, `proposal_not_refileable`, or
`proposal_target_unavailable`.

`POST .../edges/{edge_id}/refresh` (body `{"evidence_rev": int}`, the
Evidence revision the caller saw; the id may contain slashes) returns the same
shape. It admits the same light chat on the edge's Evidence. Its key is derived
from the edge id and that revision, so every repeat returns the first chat, even
after the agent removed the edge. Before that chat exists, an absent edge or one
that is not a [stale assessment](graph-history-and-transitions.md#stale-assessments)
returns 409 `edge_not_stale`, and a revision the Evidence has moved past returns
409 `edge_evidence_moved`.

## Application surfaces

### Overview

Overview shows current project state and latest plain-language revision summary.
History names the canonical list **Project revisions**, attributes new records
from their stored snapshots, labels legacy records **Unattributed**, and derives
truthful operation fallbacks without inventing causality.

### Inbox

Inbox contains pending protected-belief Proposals, Decisions in `ready` or
`revisit`, and asserted open Blockers. A Proposal keeps inline judgment because
it is not a node. Its proposed action links each existing node the Proposal
changes, both ends of a relation included. A stale Proposal card has a red edge and replaces Approve with
one icon-only Refresh, which then opens the refresh chat; a failed Refresh turns
the icon red and stays the retry. A form ordinary Work cannot file, one whose
nodes are all gone, a branch target, or a staleness only a staged draft
introduces offers Reject alone. A Decision row opens the existing node-detail
ballot. That
ballot marks the prior choice on the matching option, and shows it separately
when no option matches: an agent may reword a Decision's options but never
writes `selected_option`, so a reopened ballot would otherwise not say what is
being revisited. Accepted or contested open Blockers remain graph state but
leave human attention.
Historical Ambiguities never render or count.

### Research

Research presents a question drill-down and a bounded DAG. The drill-down
follows relations, not nearest-question assignment, so a shared Hypothesis,
Experiment, or Evidence appears under every question that reaches it, and a
Hypothesis page links its other questions. Inputs include Decisions attached
by `has_decision`, Decisions governing its Experiments, and Blockers stopping
those Experiments, with shared Decisions deduplicated. A Hypothesis attachment
alone never gates an Experiment. A pure Experiment input Decision needs no
Hypothesis attachment. Each Experiment's Evidence carries its verdict on that
Hypothesis; the Hypothesis tally also counts Evidence no listed Experiment
produced. Historical Question→Decision, Question/Decision→Blocker, and
`requires_decision` paths remain visible, including their one-hop question
counts. An Experiment that tests no Hypothesis remains reachable through the
question's attached Hypotheses' inputs or historical Decision/Blocker paths
that it or its Evidence touches. The trail is session-scoped per project and
graph target.

Relation choices offer only authorable relations and legal endpoint types.
Existing legacy edges still support inspection and removal; history readers do
not require their relations to appear in the current authoring choices.

The DAG's Research flow uses one column per node type, ordered ResearchQuestion, Hypothesis, Decision, Blocker,
Experiment, then Evidence. All question depths share the ResearchQuestion column;
relations affect ordering within a column, never type placement. Columns start at
the same top row. Manual pins remain explicit position overrides.

The node detail is a persistent, resizable, viewport-clamped inspection window.
Its stable vertical one-hop relation map shows incoming neighbors, focus, and
outgoing neighbors without a nested scroll area. At most two comparison windows
remain open. Full-screen relation inspection does not navigate or add authoring
authority. Entering Agents closes node detail.

### Runs

Run requests expose `code_worktree` and `graph_isolation`. Auto-research resolves
omitted/null code isolation by eligibility and requires graph isolation;
Experiment defaults both off and retains an existing branch's owner. Explicit
ineligible isolation refuses. Only the API Auto-research start resolves the
code default; disabled internal admission does no manifest/Git probe. Responses
publish both choices and `isolation_owner_episode_id`; Resume, Retry, and Add N
turns retain them. Clients never supply paths or ownership.

Runs lists durable Experiment-loop and Auto-research parents, using backend
health, section, recommendation, and control offers. Experiment entries use
`ExperimentControlState`, never generic episode status. Layout belongs to
[Projections](interface-and-visual-design.md#projections). Seed/Refresh and other
tasks remain in History; Blocker judgment stays in Inbox.

The exhaustive episode projection gives endings precedence over active state,
then wrap-up. Exhaustion/human pause needs reauthorization; revoked login needs
sign-in; stopped repeated recovery offers a changed provider/model/reasoning.
`blocked_reason` identifies the clearing action or is null. Auto-research detail
can include the cause in its recommendation and show a diagnostic; controls
appear only when valid. An ended Experiment whose human-authored node is closed
is Completed with no fresh Start until reopened. Report selection uses the newest
report-bearing episode on the exact target: `report_is_current` distinguishes
Open report from Previous episode report. A newer reportless episode cannot hide it.

Continuation fields and ordered `chain` publish every member's ceiling, usage,
ending, and report. One card represents the chain; Add N turns follows
`can_continue`, and timeline boundaries mark continuations. The branch root owns
`episode_id`; its newest member is `current_episode_id`. Settled `not_started`
wrap-up means no report was needed; sign-in-blocked reports remain wrapping up.
When reauthorization is recommended but `can_continue` is false, expanded
Auto-research detail asks the member to review the episode rather than suggesting
an unavailable action.

`GET /api/providers/logins` lists every login, shared and own, on each space
machine and project target host, with secret-free credential summaries, labels,
supported sign-in methods, the space machine names on that host, and active
sign-ins ([row rules](providers-and-containment.md#team-execution-accounts-and-credentials)).
The project snapshot's `provider_logins` lists, for the viewer, the login each
agent's launch would use: each execution profile's provider on the base machine,
resolved through the viewer's setting. Parked work (queued tasks,
provider-auth recoveries, the current failed Experiment turn, undelivered
watcher groups) adds its provider and host, resolved the same way; a parked
Codex session keeps the login it is bound to. A refusal that names no login lists
every login of the refused source. Two members can therefore see different rows.
Apart from parked work, a machine no profile runs on raises no notice, and a provider whose login RCP
does not manage adds no snapshot row. Runs merges these rows with the live Settings list, so a sign-in
there clears the notice at once. The project landing instead reads the live list's
rows marked `agent_used`, which spans every project the viewer belongs to, so it
has no missing-own-login row.
Each signed-out login raises the notice described with the Sign-ins fold below;
its icon-only dismiss hides that sign-out on this device until the login signs
out again (a missing-own-login row, until an own login exists). Space Settings offers only the backend's methods: device-code flow
with polling and a Cancel icon for its starter, one-shot token entry verified by
a real request, Check sign-in, Sign out, and, on added shared logins, Remove
from shared, confirmed once with the login's running count. Check sign-in is hidden while a token is unsent or a token-only
account has none saved. Provider names never select UI behavior.
Generic routes reject unsupported actions/fields; GET only reads. Successful
verification reconciles parked work even after leaving the page, with durable
repair and no duplicate launches. Any signed-in member may act on a shared login;
only the owner acts on an own login. Durable actions record the member.

Personal macOS Space Settings reads `GET /api/machine-power`: idle-hold toggle,
demand/status, opt-in lid-mode install, and Uninstall. `PUT` changes `idle_hold`
or `lid_mode`; enabling clears a latch. Install/uninstall are POSTs; cancelled
admin prompts return unchanged status with 200. Landing warns only for a latch
or cleanup failure, with the exact recovery command. Power policy belongs to
[Keeping a Mac awake](server-and-machine-operations.md#keeping-a-mac-awake).

**Why:** The backend knows demand and outlives the window. Lid mode extends idle
hold with one watchdog executor and revocable generations, avoiding a resumed
backend re-arming a released flag. The narrow sudo rule supports unattended
unsigned apps; a boot reset clears the persistent flag. RCP must enforce battery
and thermal release itself, and closed-lid release explicitly sleeps even with
an external display. Coarse episode demand favors preserving runs over precise
battery accounting. The enrolled account can invoke the same sudo commands;
removing the rule can strand the flag. Install/uninstall or reboot may clear a
user-set flag, and uninstall has no independent lock if the backend dies.
These controls do not guarantee network, provider, or remote-host availability.

Each Experiment target contributes only the current episode chosen by its control;
older episodes stay in History. Cards identify target, checkout, starter (a member,
Auto-research, or the Work chat that started it, linked), and historical human
authorizer; missing attribution never borrows the viewer.
Selection, Stop, and busy state bind the exact episode. Other live target loops
are informational in Run, not a launch veto. Main-loop chat opened from a branch
is read-only. Chat watcher strips show only their own/live target rows; ended
watchers remain in Runs.

[Episode archive](conversations-episodes-and-watchers.md#episode-archive) is shared.
Archived entries leave default cards/counts/child links; Show archived adds a
separate section, retaining each episode's identity, History, and Unarchive even
outside recent/space windows. Storage refreshes archive metadata on cached controls.

Both `/api/episodes?mode=experiment_loop` and project
`/api/projects/{id}/experiment-episodes?mode=experiment_loop` return `{entries, unavailable}` with
non-null current episodes. Main entries use completed controls; branches use
exact branch read models. Unreadable live or needs-action loops yield
identity/target/node/episode/diagnostic rows without hiding healthy loops; other
historical failures log and skip. A project whose required cache is invalid,
missing, or of the wrong JSON shape makes only its main loops unreadable.
`/api/space/runs` lists each such unreadable loop as a degraded
"Unavailable" row beside the healthy runs, so one project never empties the
space ledger. Task rows publish role/depth rather than client parsing.
Project lists paint cached data, share in-flight reads, and refresh while Runs
is visible. A child Experiment appears as one subordinate navigation actor on
its orchestrator's timeline, never a second budget/lifecycle; it gets a Runs card
of its own only while a route opens it, and the space ledger never lists it.

### Episode timeline

`GET /api/projects/{project_id}/episodes/{episode_id}/timeline` is a read-only
actor projection shared by Auto-research and Experiment run detail. It returns
`episode_id`, `mode`, `generated_at`, `truncated`, continuation-chain `members`,
and typed `actors`, `spans`, `handoffs`, `messages`, `signals`, and `marks`.
Actors carry stable identity, kind, label, row key, lifetime, outcome, recorded
starting span, and navigation links. Spans carry turn/attempt/report kind,
times, status, attempt and invocation numbers, nullable cause/error/headline,
and task and episode ids. Headlines are bounded first sentences from stored
answers; reports have no headline. The browser groups actors by `row_key` and
derives summary counts, relations, and per-turn wake details from this
single response, without a parallel event-list model.

Hand-offs join recorded admission commands to their starting span and worker or
Experiment actor. Messages retain sender and recipient, sent and delivered
spans and times, and one disposition: `wake`, `harvested`, `cleared`,
`failed_attempt`, `undelivered`, or `unknown`. Delivery records allocation,
never proof of reading. Signals contain inline notice or watcher payloads,
source actor and row, landing span/time and `woke`, `harvested`, or
`acknowledged` landing mode, plus recorded arming provenance. Graph watcher
sources name the node row, not an inferred episode. Lifecycle marks preserve
nullable issuing spans; missing provenance is never guessed.

Joins resolve across the full continuation chain before bounding the response.
The newest 400 spans, hand-offs, messages, signals, and marks are retained;
actors remain when a returned span or item references them. `truncated` means
summary counts cover shown items. Referenced actors or spans may be outside the
returned set; the chart places missing endpoints at the window edge. In
`experiment_loop` mode the roster contains the human, Experiment agent, and
shell watchers grouped by their row keys; retries and reports remain spans on
the agent row. Auto-research's graph watchers stay on its timeline; child shell
watchers stay on their Experiment timeline.

`GET /api/projects/{project_id}/episodes/{episode_id}/timeline/text/{text_ref}`
loads immutable full text on demand for `handoff:<command_id>` or
`message:<message_id>`. It returns `text_ref`, `kind` (`assignment`, `goal`, or
`message`), `owner_episode_id`, `body`, and `sha256`, checks existing project
membership and the requested continuation chain, and returns 404 for records
outside that chain. The roster response carries only bounded text previews.

The message composer sits beneath the roster. It links turn and attempt spans to the existing task inspector,
excludes reports from that navigation, and resolves child Experiment navigation
by episode id. Watcher controls remain with their existing owners. Layout,
selection, and gesture behavior follow the
[interface specification](interface-and-visual-design.md#auto-research-and-episode-history).

Experiment detail links active turns to the task inspector, labels durable
objectives separately from stale guidance, and shows `parent_watching` from the
backend. Its watcher fold contains only work that child handed off. Starting Run
opens this detail, separating historical budgets from Next episode limit and
showing watcher/session/host continuity. An explicit main route must match both
episode and target; an advanced control shows a History handoff, never the newer
episode's controls/transcript.

Isolation-owning cards show branch base/head, merge state, and the server-offered Merge.
Submission rechecks current eligibility; [merge projection](auto-research-and-branch-merge.md#runs-projection)
owns admission. Run offers On a new branch and Code worktree, off by default;
existing branches hide the former. Shared Branch name accepts blank for the
server date name. Auto-research on main uses the same name plus server-eligible
code choice; on its branch Continue sends mail or adds turns with a message.

Branch DAG nodes show one merge disposition (Conflict, Needs agent, Proposal,
Already merged), else Added/Changed/Removed. Standing/status/connect appear on
hover. New nodes show a tag, omitting redundant field lists unless merge needs a
decision. Edited nodes show visible changed fields plus marked internal fields,
with main-before, branch, and competing main-now values. History names each
accepted node change and links its task. Open graph selects the branch workspace.
Historical branch Experiment routes expose transcript without an ordinary
composer; independent branch chats retain their own sessions/authority.

### Agents

Agents (route view `chats`) groups project and node conversations. Every human and assistant turn
keeps its immutable Discuss/Work label; progress stays inline under the triggering
message. There is no global task banner. The composer and history remain usable
while unrelated background tasks run. A running watcher wake or episode turn has
no human message, so it holds an Activity row in its chat until its first output.

A conversation header carries the agent's live status from its current turn,
or its latest finished one: a running-subagent count, a compaction count, and a
context ring of tokens used against the window. Any of them opens one popover
with Context, Subagents, Compactions, and This turn; at phone width the header
keeps the ring and the popover opens as a bottom sheet. A window the current
turn has not reported yet comes from the chat's latest earlier turn with the
same provider and model. The chat route joins each assistant turn's status
into the transcript, so an older turn outside the bounded task list still
counts. A compaction also leaves a divider with its before and
after tokens in the turn where it happened. A runtime that reports no context
shows no ring.

Agents lists every conversation in the project: chats on main and on every graph
branch, Experiment episode conversations, Auto-research orchestrators and their
children, and conversations whose first turn has no transcript yet.
`GET /api/projects/{project_id}/chats?inventory=true` returns that whole inventory
as one snapshot from the retained main service's cached scan, so a conversation
updated between requests is never skipped; each summary carries its graph
target, a graph title, and a `conversation_kind` of `chat`, `episode`,
`auto_research_child`, or `auto_research`. The Agents tab shows only the
selected graph's conversations, plus, on main, each listed Auto-research chain's
orchestrator ([interface](interface-and-visual-design.md)). The Archived
column starts folded. Read markers cover the whole project; unread finishes cover
the selected target unless the read asks for the whole inventory.

Selection stays exact-target. A link to a conversation on another graph switches the
workspace through `#/projects/P?view=chats&chat=C&branch_id=B` before the
composer mounts. A link whose `branch_id` is wrong or missing reroutes to the
graph the inventory records for that chat; a chat no graph lists selects nothing.
A `branch_id` that is empty or that the backend does not know opens main with an
error notice. An orchestrator selected from main's Agents opens in place
against its own branch target without switching the workspace. Node actions select only within the viewed graph. A
conversation a task names before the inventory lists it has unknown ownership:
its composer stays closed and the inventory is fetched again. Posting
follows the ordinary chat rules on that target. While its orchestrator lineage
runs, an Auto-research child's chat is read-only and its attention belongs to the
orchestrator; once the lineage ends, the human may continue it as an ordinary
chat with ordinary attention. A read-only child shows its transcript and a link to the orchestrator in Runs,
and task admission refuses a human turn on it with
`auto_research_child_read_only`.

An Auto-research continuation chain is one `auto_research` conversation on its
branch, identified by the chain root's episode id (the branch id). Its stored
agent name starts as the branch name; subsequent naming follows
[agent names](conversations-episodes-and-watchers.md#agent-names). Its transcript
is projected from the chain's orchestrator-role
turns and the artifact edits that reply to the chain: each turn's starting
instruction or the human mail it delivered, then its answers; worker turns keep
their own cards. An artifact edit with a `chat_id` belongs to that chat's card and
refreshes its order and status, but never creates a card by itself. Loaded turns
supply live status and progress, and the episode's `needs_action` health (an open
question) files the card under needs-you. While the chain runs, the card's
`human_turn_refusal` is `auto_research_orchestrator_running` and its composer,
with the episode's open questions, sends **Message orchestrator** to the newest
chain member, gated by that member's `can_message`, `message_refusal`, and
`message_requires_continuation` exactly as Runs is. The composer explains
`episode_merge_reserved` and `episode_isolation_unavailable` refusals. Once the
chain ends, the card has the ordinary chat composer and posts project-chat turns
to its own branch, even when opened from main; those turns join the card and its
transcript beside the orchestrator turns. Add N turns stays in Runs.

The composer shows **Work in a worktree** before the first Work turn. The backend
projects eligibility and the reason a zero/multiple-repository or wrong-machine
scope cannot bind. A bound chat shows the real branch and path, an **Integrate**
menu with backend-resolved branch names and refusal reasons, and an explicit
**Remove worktree** confirmation with ahead and remote-branch evidence. These
controls dispatch the ordinary task API with a worktree choice or integration
choice; RCP supplies the integration instruction and target. Unsent drafts remain
intact when an integration turn is dispatched. A removed binding remains visible
and cannot silently become a shared-checkout chat.

`GET /api/projects/{project_id}/tasks` returns the newest tasks up to the list
limit, plus the latest turn of every chat whose latest turn is still running or
waiting on a person (queued, running, pausing, paused, failed, or interrupted),
up to `AGENT_TASK_LIST_OPEN_CHAT_LIMIT`. A chat that needs a human therefore
stays visible however many newer tasks exist; a failure followed by a later turn
in the same chat does not count. Such a latest turn also stays listed for
`AGENT_TASK_LIST_FINISHED_CHAT_SECONDS` after it finishes, so a client sees the
terminal record.

Chat task responses include `current_chat_session_id`, resolved from server task
metadata independently of the displayed historical task. NodeChat and WebMCP
use that projection, never a search through task or transcript native-session
ids. Admission re-resolves and binds atomically; a client hint cannot pin a new
ordinary turn to a superseded session. Artifact comments keep their artifact's
ownership checks while following the chat's current session profile.

Read state is stored per user on the server, so it survives a reload, a closed
app, and a second device. `GET /api/projects/{project_id}/chat-reads` returns
the acting user's markers, one finish time per chat, plus a `baseline`: the
time the markers were introduced, which stands in for any chat without one. It
also returns `latest_finished`, each unarchived chat's newest finished turn on the
selected target (every target with `inventory=true`), so a
reply older than the task list window is still found. A chat is unread when its
newest finished turn ended after its marker. Viewing a chat
posts its newest finish time to `POST .../chats/{chat_id}/read`; the marker only
moves forward. Markers are not moved with a transferred project.

### Paper, Settings, and History

Paper owns the co-edited file tree, its previews, and coach change
cards ([Paper](paper-artifacts-and-result-views.md)). Settings has two
levels. Space Settings, opened from the gear beside the identity menu, owns the
server status (team spaces), machine cards with their names (renamed in place)
and their Sign-ins fold, and the personal space's clear-all-caches. Project Settings owns
repositories, which space machines the project uses (the same machine cards,
read from the space), execution profiles, packages, caches, project
membership, and prospective episode limits, not ontology authoring. Display
preferences live in the identity menu.

Project memory has no Settings screen; members manage notes through project chat.

Settings' Repos rows show each repository's **Agents can write** box, checked
for members of `project_truth_scope` and fixed on for the state repository.
`PUT /api/projects/{id}/repositories/{alias}/writable` (`{writable}`) appends the
human approval that changes that membership and returns the project snapshot.
A personal project's Add repo form posts a folder to
`POST /api/projects/{id}/repositories` and has no GitHub mode; a team's uses the
repository request below;
`PUT /api/projects/{id}/settings` no longer takes a default run scope, and no
composer or launch dialog selects repositories, except the single writable
repository a worktree uses.

Team repository descriptors include `source` (`github` or `server_only`),
nullable `github_identity`, and `can_connect`, from `effective_repositories`;
only a server-only team repository has `can_connect` set. The display
completion path reads this operational provenance afresh even for cached graph
snapshots, so a completed Connect is immediately reflected. Personal
repositories, and team repositories whose provisioning evidence cannot be
resolved, omit all three fields: nothing claims a server-only state or offers
Connect without proof. These fields describe RCP-managed provisioning, not
discovery of a personal checkout's Git remotes.

Each machine card's **Sign-ins** fold shows one raised panel per provider. Inside
it, two sunken trays, "Yours" and "Shared on <machine>", hold raised sign-in
cards; left to right is the order the member's work tries them, and at phone
width they stack top to bottom. Each tray header has a switch and a round swap
button sits between the trays; each change writes the setting at once:

| Stored value | Trays in order | Yours switch | Shared switch | Swap |
| --- | --- | --- | --- | --- |
| `own_then_shared` | Yours, Shared | on | on | stores `shared_then_own` |
| `shared_then_own` | Shared, Yours | on | on | stores `own_then_shared` |
| `own` | Yours, Shared | on, locked | off | disabled |
| `shared` | Shared, Yours | off | on, locked | disabled |

Turning the other group back on stores "the tray that is on, then the other". An
off tray stays visible, faded. A ＋ slot ends the Shared tray, and the Yours tray
while the member has no own login; adding starts a device sign-in at once or
opens token entry on the new card. The machine's card is named "Machine sign-in";
an added shared card is named by who added it, with the date on hover; the
viewer's own card is unnamed, and a Members card is named by its owner. A card shows, first match wins, Being removed,
signing in (device code and link only to the starter; others see who), Signed out
(flat and dashed, with no label; a Sign in button on the viewer's own and shared
cards), Usage limit (amber edge; a limited login is still used
when nothing else is free), or Ready. A failed action marks the card or panel
with a red edge and one Retry icon whose hover is the error. Only the viewer's own and shared cards act: apart from Sign in, their actions sit
behind a ⋯
shown on hover and always on touch screens and at phone width, hidden while the
card signs in, drains or takes a token. Explanations are hover text only.
Personal and team spaces draw the same fold; a provider whose login RCP does not
manage, such as OpenCode, shows only its machine sign-in card, without state or
actions. A collapsed Members row, shown when the space has other members, counts
their signed-in own logins. A signed-out login's notice is one row: key mark,
provider, the host when remote, an icon (hover "Sign-ins") that opens this fold,
and Dismiss. On Runs, work waiting on another member's own login names them
instead of the icon; the landing page omits those rows. A failed list load marks
the fold itself.

`/api/space/machines` lists, creates, configures, and deletes machine cards. A
card view carries its settings (`job_manager` of `slurm`, `none`, or null while
undecided; `jobs_root`; `access_hint`; `provider_paths`), the provider rows with
cached readiness, the route probes of any project using it, `agents_run_here`
(some project's agents run on it, so its helper jobs launch there), and `reach`:
the last on-demand check from each machine an agent runs on.
`PATCH /api/space/machines/{machine_id}` changes only the fields given; an empty
provider path forgets it, and a compute change clears the card's probes and
re-checks them before it returns, so the returned card shows the new routes.
`POST /api/space/machines/{machine_id}/compute/check` re-runs the route probes
and reach now. Both settle an undecided job manager, even on a card no project
uses yet. `POST /api/space/machines/{machine_id}/providers/{provider}/resolve`
finds the executable on the machine and records it on the card. A card carries a boolean
`deletable`: true only when every project can be read and no repository
lives on any alias bound to it, including each project’s base. Delete detaches
the machine from every listing project before deleting the card. If detaching
fails, the error is returned and the card stays. Host and account never change.
A team create with a blank account fills it
from the login check or refuses with 422 and a `detail.code` of
`authentication_failed`, `host_key_unknown` (with the fingerprint),
`host_key_changed`, or `unreachable`. `POST /api/space/machines/host-key` takes
`{host, fingerprint}`, rescans, and appends the key to the route's first
`UserKnownHostsFile` only when the fingerprint still matches and no key for that
name is recorded. A `PATCH` of `writable_paths` validates each
path on its machine: absolute, an existing directory, no `:` or `$` or control
characters, not `/`, and not inside RCP's own storage.
A `PATCH` of `provider_autocompact` merges the given providers into the card's
map of provider id to auto-compact setting; each provider profile validates its own value (Claude:
`auto` or a token window, rendered as `--autocompact`; Codex: a token count,
rendered as `model_auto_compact_token_limit`), an empty value restores the CLI
default, and a provider without the setting is refused. The launcher reads the
card at every launch on that machine, so an edit applies from the next turn and
is not recorded with any turn.
A `PATCH` of `provider_shell_timeout` uses the same per-provider merge and empty
value removal. Claude, OpenCode and Codex accept whole minutes from 2 through
120. The card exposes `shell_timeout_providers` entries with `provider`, `label`
and `default_minutes`, alongside `autocompact_providers`, and uses one setting
row component for both fields. Unset shell timeouts keep the provider default;
the resolved value also sets the ask hold, with a 30-second margin.
`/api/space/machines/{id}/directories` lists one directory level on the machine,
filtered then paged, marking protected entries; project setup's folder browser
uses the same endpoint. `GET /api/space/machines/{id}/browser` reports that
machine's agent-browser readiness, reaching the host, so cards load it one
machine at a time rather than with the list; `POST .../browser/install` is the
explicit, bounded install. `POST /api/projects/{id}/machines` appends a machine
alias to the project manifest through the state workspace. Existing projects
fill the machine list at startup and on registration. Project
snapshots expose non-secret compute metadata; readiness exposes a backend-owned
execution-machine/target-machine matrix with distinct unreachable, authentication,
and host-key states. Normal readiness reads reuse the last result; only an
explicit refresh runs network probes, and a machine route or execution-binding
change invalidates the cached matrix. Settings writes never accept private keys
or passwords.
History and task detail own
complete provider attempts, stages, events, diagnostics, package versions,
answers, graph outcomes, and recovery chains.

In a team space, Settings also reads one authenticated `/api/server-status`
projection. It carries backend-owned labels and presentation tones for the
running/current/managed/upstream release relationship with the running,
installed, and latest release versions and the release check time and any
failure reason, update
readiness, the
latest backup attempt and latest independently retained protected-archive
receipt, protected and uncaptured project counts, completed-restore age,
installed machine-tool readiness, and private provider-check availability. The
browser formats timestamps, byte counts, and commit abbreviations only; it does
not reconstruct operational state from raw status vocabularies. An unsafe
concrete doctor, backup-receipt, or restore read fails visibly instead of
becoming an empty or healthy panel. Each `rcp server` command it shows is the
operator's own line (`rcp.operator_commands`): `sudo` for root work and
`sudo -u <service account> -H` for the rest.

That route is GET-only and available only to an authenticated team member.
`?refresh=true`, sent by the panel's refresh button, only shortens the release
lookup bound to 1 minute. It
does not configure or run backup, update, restore, Git credential preparation,
project provisioning, provider login/check, or member removal. It also does not
enumerate the console operations that do: an operator holds machine authority
already and reads `rcp server --help` on the machine, so restating that
catalogue in a member-visible panel adds a second place for it to drift. A
command still appears in the Web surface where one specific request needs it,
carrying that request's own identifier. The desktop operator
bridge remains the only native launcher and accepts only its fixed
project-provision command.

## Causal and relation presentation

Causal layout is a read-only derived projection from the same graph revision.
Feedback strongly connected components rank together; layout never generates a
graph action or breaks a cycle by changing truth.

Evidence node detail presents observation, interpretation, role, validity,
origin, provenance, and artifacts. The Hypothesis relation/detail surface owns
each claim-relative direction, relevance, weight, scope, and qualifications.
Historical global strength is clearly legacy and never shown as current edge
weight.

[Glossary presentation](paper-artifacts-and-result-views.md#glossary-presentation)
owns inline definitions.

## Desktop shell

The desktop window is a client of backend-owned durable work. Closing or hiding
the window never cancels a task. Reopening attaches to the healthy owner and
current app state. Closing a full-screen window first leaves full screen and
keeps the window hidden once macOS finishes the transition, so no empty
full-screen Space remains.

No link strands the app on a page it does not serve. Rendered Markdown keeps
node, repository-file and in-app `#/` links; external `http(s)` and `mailto`
links open a new tab, and any other link renders as plain text. A link the page
did not handle that would leave the app document opens a new tab instead, and a
page navigation to a path the server does not serve redirects to the app.

A source-mode frontend rebuild preserves content-hashed assets from the prior
build so an already-open window can keep lazy-loading its coherent bundle until
the researcher reopens it. Packaging and server startup use a clean build. If a
chunk is nevertheless unavailable, the client performs one bounded document
reload; a repeated failure renders an explicit reload action instead of a blank
window or reload loop.

The shell keeps the stdout and stderr of the backend it owns for the whole
run, not only until launch, in `backend.log` in the app's log directory. Each
line carries a UTC time and its stream, and owner sign-in codes are redacted.
A marker line names each run's start and, when the shell sees it, its exit. The
file rotates at 5 MiB and keeps three older files, so the set holds at most
20 MiB. An adopted backend's output belongs to whoever started it. A failed launch still writes its whole capture to
`backend-launch-failure.log` and names it in the dialog.

App Quit gracefully asks only the backend process this shell owns to pause
recoverable work and shut down. It never kills a reused backend or unrelated
process. If graceful timeout is exhausted, the shell reports the forced path
truthfully. Singleton replacement and frontend build ownership stay in the
launcher, not manual PID cleanup.

Artifact, report, and repository-file previews open in one panel inside the main
RCP window. The desktop has no native preview-window commands, and same-origin
popups create no window: a recognised artifact or report URL opens in the main
panel, and any other popup is dropped and logged. Native downloads resolve
through shell-controlled destinations. A PDF artifact opens in the system PDF
viewer through one main-window command that takes only project, task, and
artifact ids, fetches the artifact's Download route itself, checks that the
bytes are a bounded PDF, and writes them to a private app-owned temporary
directory; failed opens are removed at once and copies older than the named
one-day `PDF_PREVIEW_RETENTION` are pruned on startup and each open. It
never opens an arbitrary path or URL and remains restricted to the main window.

In personal project setup, every local repository path has a native folder
action in the desktop shell. Selecting a folder fills its absolute path;
cancelling preserves the current value. An SSH repository keeps its manual
absolute-path field and also offers a backend-owned bounded browser in both Web
and desktop. The browser uses only SSH credentials already configured on the
machine running RCP, opens at the authenticated remote user's home, and lists
one directory level per request without recursive discovery. It labels direct
children that contain `.git` or `.research`. An authentication or host-key
failure names the exact RCP machine where ordinary SSH state must be repaired;
RCP never accepts or stores a password, private key, or credential path. Team
project provisioning continues to use server-managed checkout paths instead of
this personal path browser. Every SSH destination is centrally rejected before
OpenSSH when it is empty, option-shaped, or contains unsupported characters.
Editing the repository location, host, or path aborts and invalidates an
in-flight browse request, so a late response cannot replace the new target.
Strict host-key browser calls use a direct OpenSSH transport with connection
sharing disabled, so an existing multiplexed master cannot bypass the check.

The desktop may add shell-only dictation, update, reconnection, and packaging
behavior only where a native test owns it. Browser verification
does not stand in for native window lifecycle.

A local Codex thread created through RCP's app-server runtime is stored by Codex
and may therefore appear in the Codex Desktop task list. RCP uses that as an
inspection surface only. Sidebar ordering, loading, takeover, and concurrency
remain Codex Desktop behavior rather than RCP product state.

### Dictation

The composer dictates through **macOS** (desktop app only) or one of the
member's **service connections**, chosen in the member's own Dictation and
voice card in Space Settings. Network services work on the desktop app, the team
browser app, and the team phone web app; a personal paired phone stays
notify-only. A client whose member chose macOS outside the desktop app shows the
microphone disabled with a pointer to Settings. One microphone owner refuses a
second holder.

A connection is an OpenAI-compatible server (OpenAI, Groq, or a custom base URL)
or Gemini. Each member's connections, keys, and selection live in
`service-connections/<user_id>/` under the data directory, written privately
and atomically under one per-member lock that rechecks membership. Keys never
appear in a response, a validation error, or a log. For dictation, Connect
transcribes two bundled clips recorded from real `MediaRecorder` output
(WebM/Opus and fragmented MP4/AAC) of a short spoken phrase, and saves the
connection only if one returns a non-empty transcript, recording the accepted
formats. An empty transcript fails the check, because a model that cannot read
the reply or the audio looks the same as silence. The connect card does not ask what a service
is for: the Web connects with the `transcription` purpose, and the member picks
uses afterwards under **Dictate with** and **Runs on**. Picking an OpenAI
connection under **Runs on** adds `voice` and runs the voice check then. The
API still accepts `voice` at Connect, where a voice-only key skips the clips.
A custom base URL must be `https`, or `http` to loopback.

**Why:** Dictation billing and voice data belong to the member, unlike a shared
execution-account provider login. Backend calls keep service keys out of browser
scripts and provide one path for desktop, browser, and phone. Connections stay
outside backups and must be reconnected after restore. The service account can
read them; custom URLs have no internal-address fence because members already
have that network reach through terminals.

On a macOS or Windows computer, the Connect card opens by naming the keyless
route: a dictation shortcut set in the ChatGPT desktop app types into the
composer like any other text, so RCP needs no connection for it. ChatGPT's Linux
app and phones cannot type into other apps, so they never see it.

One card connects a service and later edits it. It holds every model the
service has, each with what it does: the dictation model, and for an OpenAI
key the live voice model and the thinking (delegation) model. Each model
is a dropdown of the provider's current ids plus **Other…** to type one.
`POST /api/service-connections/models` lists them for a key not yet saved, and
`GET /api/service-connections/{id}/models` with a saved connection's key. Both
return `{transcription, delegation, live}`, read live from the provider's `/models`
list, which carries ids but no capabilities, so names choose the candidates:
dictation ids contain `transcribe` or `whisper` and are not `diarize`, `live`,
or `realtime` streaming models; delegation ids are OpenAI `gpt-4` and later,
newest first, without dated snapshots or transcription, speech, realtime, live,
image, search, or instruct models; live ids start with `gpt-live` and are not
transcription models. Ids with a `shutdown_date` are hidden. Gemini lists only
its `generateContent` transcribe models, because RCP sends audio without an
instruction; a custom server lists every id, named ones first. A failed listing returns `model_list_failed` (502), or `service_access_denied` when the provider refuses the key, and the card falls
back to a text box with that reason. The save check stays the authority.
Connect takes `live_model` and `delegation_model` for an OpenAI key, with or
without `voice`, and checks each changed one with `GET models/{id}` using the
key of the connection that runs voice, or its own key when none does. Giving a
connection `voice` checks both current values with that connection's key. A
value or voice connection changed by another request during a check returns
`connection_changed` (409). The card lists models only
after a key is entered, never sends a key to another service, and lists a
custom server only once its address field is left. `PUT /api/service-connections/{id}` takes
`{purposes, model, live_model, delegation_model}` and checks only what changed
with the stored key: a new dictation model reruns the clips, a new voice model
reruns the lookup. Omitting `purposes` keeps the stored uses, so a model-only
save from the card never replays uses changed elsewhere. A voice model holding
the edited connection's key or the voice payer's key is refused before any
lookup. The key cannot be edited; a new key means disconnecting and
connecting again. The voice models stay member voice settings, written under
the same lock as the connection, because one connection holds `voice`; two
OpenAI connections show and edit the same values.

`POST /api/service-connections/{id}/transcribe` takes one raw audio body of an
accepted format, bounded by `Content-Length`, the bytes actually received, a
read deadline, and a per-member concurrency limit, and holds it only in memory.
Outbound calls skip proxies and redirects and read a size-capped response under
one deadline; upstream errors are bounded and never echo the key, and a
transcript that contains the key is discarded with an error rather than
edited. The team
middleware admits the two audio types on this route only. On the personal
loopback server another site's `audio/*` request needs a CORS preflight, which
the server refuses, and a no-cors request loses its type and gets 415.

The client records with the first accepted format `MediaRecorder` supports and
pins the connection id when recording starts. Network dictation has no length
limit. One stream records back-to-back pieces, each one upload: the first ends
at the first 0.4 s pause after 5 s, so a failing service shows within seconds;
later pieces end at the first pause after 40 s, and every piece ends by 55 s.
Pieces transcribe strictly in order and append to the dictation span while the
composer shows the elapsed time and whether a piece is transcribing. While the
microphone opens, the client also lists the connection's models and waits for
the answer before recording, so the check never holds a transcription slot an
upload needs; a 401 or 403 returns `service_access_denied` (502, with the
provider's sanitized message) and dictation fails before anything is recorded,
while any other listing failure is ignored. A piece that reaches 55 s ends even
when the next one cannot start.
Transcription failures carry `service_access_denied` (401 or 403),
`transcription_rejected` (another 4xx), or `transcription_upstream_failed`
(5xx, redirect, or transport); only the last, or a lost connection, is retried
once after 2 s. A second failure stops recording.

Speech that cannot reach the draft is kept in memory per chat while the app
runs, never on disk: the failed piece and every later one, or, when typing,
sending, or leaving the chat detaches dictation, every piece still to come.
**Retry** (or **Insert**, when every kept piece is already text) continues where
dictation stopped if the draft is unchanged, else at the cursor, and transcribes
with the current dictation choice. Starting a dictation or sending asks before
kept speech is discarded. A page hidden while recording or while dictation
starts (a phone locking or switching apps) stops like Stop and says why. macOS dictation stops at 55 s: a
ring on the microphone drains over that time and turns amber, with the seconds
shown, for the last 10. Native
dictation reports `preparing` while macOS downloads the on-device model and an
`engine` (`speech_analyzer` or `apple_server`) on `recording`; every result
carries the whole session text. `desktop_stop_dictation` takes `finish`: Stop
delivers the final result before `stopped`, invalidation cancels. SpeechAnalyzer
needs only microphone access; speech recognition permission is requested only
before the older recognizer. A desktop build without the macOS 26 SDK skips
SpeechAnalyzer with a warning; release builds require it, and CI fails if any
macOS 26 Speech symbol or Swift library is a strong import.

### Update notice

Installs follow promoted `vX.Y.Z` releases. The normal macOS install is the
unsigned prebuilt app in companion `desktop-vX.Y.Z`; source installs follow tags.
The install script and Tauri updater avoid downloaded-zip quarantine; manual zip
installation needs macOS approval. Only published prebuilt apps enable the updater.

**Why:** Release alignment avoids desktop/server drift and per-merge notices;
prebuilt apps remove developer-tool setup. Companion releases preserve the server
release's fixed asset contract. Updater signatures use a separately backed-up
minisign key, not Apple signing, with `latest.json` on `mac-latest`. Team servers
need operator authority and source builds may contain local work, so neither
self-updates. Procedures belong to [desktop](../desktop.md) and
[installation](../install.md) guides.

`GET /api/update-notice` returns the release check, looked up again first when
its cache is older than 10 minutes: space, status
(`update_available`, `current`, `pinned`, `unchecked`, `failed`, `off`,
`unknown`), current and latest versions, check times, companion readiness, a
locally built download URL, and the update command. The one update surface,
shown on the project index, setup screens, and every project view, renders it:
a team space shows `sudo rcp server update`, preceded by
`sudo rcp server supervisor update &&` when the release's supervisor wheel is
newer than the installed receipt's supervisor version; a source checkout shows
`scripts/update-from-source vX.Y.Z` (with `--desktop` from a source app); a
prebuilt app without an updater shows a Download button only once the companion
is confirmed. A published prebuilt app has the Tauri updater enabled and checks it
at launch, whenever its window is shown, and whenever the release check first
reports a newer release with a confirmed companion. Its own release is offered
only by that check: the surface shows the native **Update** button, which
replaces and relaunches the app and asks before interrupting running agent
tasks, and never the Download fallback.
The native shell reports its build kind, version, and checkout through
`desktop_build_identity`, because a desktop may reuse a backend of the other
kind. A source app compares its own version even when the backend
reports `current` and `source_at_release` (its checkout `HEAD` is the latest
release commit; a later commit keeps the same base version), because its native shell can be older than an updated
checkout; it then shows `scripts/update-from-source vX.Y.Z --desktop`. A visible page polls the endpoint (30 s while `unchecked`, then 10 min,
and on becoming visible); dismissal is per release. A protocol mismatch names
the confirmed download, the releases page when the check is unavailable, or the
update script for a source build.

## Frontend trust boundary

Provider naming is a backend answer. Readiness exports each provider's runtime
choices and its default, and a task and a Paper writing session each export the
label for the runtime they ran on, so no surface maps a durable runtime id or
picks a default itself.

The browser may stage human drafts and render backend projections; it is not the
owner of authority, tasks, graph rules, provider authentication, watcher
delivery, or canonical state. Provider implementations own authentication
mechanics for the execution account; the shared account lifecycle owns durable
state and recovery. Client-generated ids,
cached target selection, URL fragments, artifact messages, and provider output
cannot select a different project, conversation, branch, authorizer, or graph
target.

Unsent drafts kept in browser storage (graph drafts, chat messages, chat
references and staged comments, and project Settings edits) are keyed by the
signed-in member's user id, so another member on the same browser never sees
or submits them. Until a member is verified, no draft is restored or saved.
Drafts stored before this scoping have no known owner; the web client deletes
them on load instead of restoring them.

## Client error log

The web client reports what went wrong on screen to `POST /api/client-events`,
so a failure the person saw leaves a server-side trace. It sends one event for
each error notice, each uncaught window `error` or `unhandledrejection`, and
each root render failure. The send is best-effort and silent: a failed send
never shows a notice, retries, or reports itself.

The route requires the same session as every other `/api` route. An event
carries only its kind, the error message and the current view name. The backend
cuts the message to one line of `CLIENT_EVENT_MESSAGE_MAX_CHARS`, refuses any
other field or a view that is not a bare name, and writes one WARNING record on
the `rcp.client` logger with the kind, view, its own release build, the member's
user id and the message. Nothing is stored. Each member gets
`CLIENT_EVENT_RATE_MAX_EVENTS` per `CLIENT_EVENT_RATE_WINDOW_SECONDS`; the first
event past that logs one limit line and the rest of the window answers 429
unlogged. The records reach stderr, which is the service journal on a team
server and the [backend log](#desktop-shell) on the desktop.

## Provider sign-in resume response

Verified sign-in, token save, and successful device sign-in retain
`resumed: { "checked": N }`. This is a coarse count of episodes inspected
by the ordinary periodic reconciliation pass, plus queued tasks and completed
watcher groups checked for the verified account. It is not a launch count:
already-settled episodes and inputs still waiting on other conditions may be
included. The web reports items rechecked for resumption.

### Versioned live artifact data

`GET /api/projects/{project_id}/artifacts/{artifact_id}/versions/{version_id}/live`
accepts no source declaration or file path. Project membership and the stored
graph target are checked for each read. The returned `rcp-live-data` envelope
contains ordered snapshots, completeness, final/static state, a diagnostic when
applicable, and the refresh interval. Saved final bytes are authoritative for
that version. Invalid declarations expose their stored reason as static data.
A `head` query parameter names the head of the shell's cached complete
graph; a matching head returns that snapshot as unchanged after rechecking
bindings.

`/api/projects/{project_id}/research-views` lists (`GET ?branch_id=`), pins
(`POST {artifact_id, branch_id}`), and unpins (`DELETE /{artifact_id}?branch_id=`)
the acting member's pinned views for one graph target. Writes are idempotent and
recheck membership; pin eligibility, the limit, and Keep commit together. Artifact
state reports `pin_branch_id`, its display name `pin_branch_name`, `can_pin`,
and `pinned_branch_ids` for the acting member. A project route with `view=artifacts` may
carry `pin=<artifact_id>`; the shell opens that pin once its target's pins load.
Pin lists refresh through the `artifacts` watermark family.

The authenticated artifact shell pins both HTML and live reads to one version,
polls while visible, and relays through the existing private artifact channel.
The agent frame remains opaque with its existing CSP and sandbox restrictions.
The shell is frameable only by its own RCP origin. It posts one `comments` list,
each `{text, selection?}`, with `edit_now` and an explicit fresh-session flag to
the existing comments route. A conflict keeps the comments filed; success sends `rcp-artifact-edit-started` protocol
version 1 to the same-origin parent with the artifact and operation ids.

### Inline artifact presentation

The stored artifact `viewer` and `content` routes accept `presentation=inline`;
`panel`, the default, is unchanged and any other value is a validation error.
The inline content route also reads `theme` and `color_mode`, falling back to
the app's default appearance for an unknown value. The chat fixes every inline
frame's height; no frame reports or changes it. Every inline frame speaks
protocol version 1 over `postMessage`:

- chat to shell: `rcp-inline-comment-mode` with `enabled`, and
  `rcp-inline-selection-clear`. The shell maps them onto the existing
  `rcp-artifact-selection-enable`, `rcp-artifact-selection-disable`, and
  `rcp-artifact-selection-clear` frame messages.
- shell to chat: `rcp-artifact-selection` with the bounded `selection`, or
  null, and a one-line `description`.

The chat accepts these only from that embed's own same-origin shell frame.

### Artifact viewer and run inventory

`GET /api/projects/{project_id}/artifacts/{artifact_id}/state` returns
`ArtifactViewerState` under project membership. It names the current version,
its position among retained versions, and whether the storage Undo rule has a
retained predecessor. Live is null for a static or invalid declaration, live
for a valid declaration, and finished once its final snapshot is saved. An
admitted nonterminal edit supplies `editing_operation_id`.

The comment offer and fresh-session requirement reuse comments admission and
the session reservation check without launching. The reply link follows the
artifact's own chat, Experiment node chat, or Runs orchestrator thread. Viewer
URLs target stored artifacts, including reports; PDF and download-only artifacts
have no viewer URL. Download and retention information are independent offers.

`GET /api/projects/{project_id}/episodes/{episode_id}/artifacts` returns
`RunArtifactEntry[]` for the episode, including its Auto-research workers and
child Experiment episodes. It includes unexpired or kept artifacts and undeleted
reports, with the queried episode's report first, then creation order. Worker
labels use the child Work route's instruction heading, or Worker. Both routes enforce project
membership, and the inventory rejects an episode from another project.

## Bulletin channels

Each graph target has one Bulletin channel. Member-authenticated reads use
`GET /api/projects/{project_id}/bulletin?branch_id=&cursor=` and include the
project-wide roster of every non-archived Agents-board conversation and live project
members. Each roster row carries `id`, `chat_id`, `target_key`, `name`, `kind`,
`seat_node_id`, `started_at`, `state`, and `last_post_at`. The stored conversation
name matches its Agents card; `started_at` is its first turn time. Agent snapshots
and command reads keep their own channel's roster. Text is limited to 8000 characters; the server
extracts ordered, deduplicated `#node-id` references and valid
`<@member:USER_ID>` / `<@agent:ACTOR_ID>` mentions. Requests carry text, never a
separate references field. Posts carry `artifacts` metadata (`artifact_id`,
`source_name`, `media_type`) resolved at read time from the text's
`<artifact:ID>` tokens; a token naming no readable artifact of the project stays prose.
An artifact change bumps the Bulletin watermark, so open channels drop a deleted link. The
composer's paperclip opens the existing project artifact picker and inserts the
token. Each link renders as an icon and source-name chip opening the shared
artifact viewer, including its opaque HTML sandbox. Agent tokens must identify
an agent of the project in any channel, because the mention list offers every channel's agents; unknown tokens
remain ordinary text. The composer's `@` labels are unique across the project, the
shown channel's agents keeping bare names, and picking an agent from another channel
keeps the draft in the shown channel. The list opens above the composer, or below it
when the scroll area and the phone's pinned bars leave more room there.

A page holds whole threads: up to the page size of top-level posts, newest
first, each with all of its visible replies. The cursor follows the oldest
top-level post on the page, so a reply never lands on a different page from its
parent and `reply_count` matches the replies shown. Every page also carries
`newest_post_id`, the target's latest post including a reply in an older thread;
showing the newest page marks the channel read up to it. The Agents view opens on
the channel of the workspace's graph target, and picking another channel leaves
the workspace where it is. In the workspace's channel `#node` links and
suggestions use the loaded graph; in another channel a post's own references are
links that open the node on that channel's target. An agent's name or avatar opens
its chat. A sent post shows from the accepted response at once, before the
confirming read.

`POST .../bulletin` accepts `{text, key, reply_to}`. `PATCH .../bulletin/{post_id}`
accepts `{text, key}` and reparses references and mentions. `DELETE` on the same path soft-deletes the post. Only its author may edit or delete; another author
gets `403 bulletin_not_author`. A fresh edit or delete of a deleted post gets
`409 bulletin_post_deleted`; a matching successful receipt replays before that
check. DELETE uses a stable per-post key, so repeating it returns the original
success. A channel keeps at most 200 posts; a new post drops
whole threads, least recently active first, and an oversized thread loses its
oldest replies. Deleted replies and deleted top-level posts without
replies are omitted; parents with replies remain empty placeholders. Posts
include `edited_at`, `deleted_at`, `mentions`, and `reply_count`. Replying to a
deleted post is refused with `bulletin_reply_invalid`.

`GET .../bulletin/channels` lists main first, then non-archived branches ordered
by last post descending and name. Each row includes target, branch id, name,
last post time, and unread count capped at 99. Unread excludes the member's own
posts. `POST .../bulletin/read` with `{sequence_post_id}` advances that member's
target-local cursor without moving it backwards or bumping the shared Bulletin
resource version; the read client refetches channels itself.

A member mention produces at most one `bulletin_mention` notification per post
and mentioned member, including mentions first added by an edit, excluding
self-mentions. This preference defaults on. Reconciliation retains durable
observations across restarts. Deleting a post before reconciliation suppresses
its notification; editing away a mention preserves the accepted event. Links open
the channel in the Agents view, `#/projects/{project_id}?view=chats`, with
`branch_id` for a branch channel. Agent mentions never wake or launch work.

## Agent thread API

`GET /api/agent-threads` returns the acting member's lazily created Space and
project threads: `thread_id`, `kind`, `project_id`, `project_name`,
`agent_name`, `unread`, `last_activity_at`, `live_state` and
`browser_requested`. The Space thread comes first, then project threads by
latest activity. `live_state` is idle, queued, running, paused, `needs_you` (an
open question or card) or closed. Threads of projects the member left are
closed and omitted.

Thread transcript, title, read and browser routes under `/api/agent-threads/{id}`
require ownership; another member gets 404. A project thread otherwise uses the
ordinary project chat, task and question routes. Its Send must be
`project_chat` Work on main; anything else returns 422.

A Space thread sends with `POST /api/agent-threads/{thread_id}/turns`, body
`{message, provider?, model?, reasoning?, timezone?}`. It returns 202 with
`{operation_id}`, or 409 when admission refuses, such as a busy thread. Its task
reads and controls are `GET /api/agent-threads/{thread_id}/tasks/{operation_id}`
and `POST` to that path's `steer`, `pause`, `resume` and `retry`. Project chat routes enforce the same ownership, including
empty threads before their first turn. Task and question detail/control routes
return 404 for another member's private thread; lists filter before pagination.
Chat display and read projections also exclude other members' private ids.

Task rows, Patch history summaries, episode responses and Bulletin posts expose
`via_agent: {thread_id, name} | null`, derived from operational thread ownership.
This is a projection only; canonical Patch records and replay are unchanged.

Space turn artifacts use owner-checked
`/api/agent-threads/{thread_id}/tasks/{operation_id}/artifacts/{artifact_id}/{action}`
reads for `content`, `download`, and `viewer`. They share the ordinary bounded
artifact reader and sandboxed viewer. Space artifacts do not expose project
Keep or discussion controls.
