# API, Web, and desktop projections

This specification owns public project projections, current Web surfaces,
revision reconciliation, navigation and tab state, and desktop-shell lifecycle.
It does not grant graph authority; mutation routes delegate to the state
workspace and transition manager.

## Project graph-ref inventory

`GET /api/projects/{project_id}/graph-refs` returns a list of graph refs,
with main first and one row per unique episode-owned branch, ordered by newest
chain-member creation time (descending). It reads all project episodes without the recent-50 window or the episode list's archive
filter; a client needs no separate episode read to discover refs or ownership.
Existing project membership admission applies.

Each row contains `kind`, `branch_id`, `head`, `base_head`, `episode_id`,
`current_episode_id`, `archived`, and the existing branch-summary merge fields:
`merge_state`, `merge_eligible`, `merge_blocked_reason`,
`latest_successful_merge`, `active_merge_task_id`, and `merge_diagnostic`.
For a branch, `episode_id` is the chain-root owner and `current_episode_id`
is the newest chain member. `archived` is the owner's graph-isolation archive
state, independent of episode-history archiving. Merge fields reuse the episode
branch summary, including its reservation and live-writer checks.
A branch row also carries the chain root's `mode` and a display `title`: an
Auto-research starting instruction, or the Experiment's title from the main
display cache; `title` is null when neither is known.
Main has its exact current head, `archived: false`, `merge_eligible: false`,
and null branch, episode, base, and merge-detail fields. Branch ownership and
persisted records are unchanged: `branch_id == episode_id` names the root.

The project shell shows a graph picker whenever the project has a branch, on
main too, and on any branch route. It puts main first, preserves inventory
branch order, and hides archived refs unless requested or active; an active
branch missing from the list still has its own option. A branch is named by its
`title`, else a short id; the active branch shows its revision and merge state.
When the list cannot be read the picker is disabled, says so, and still names
the current ref. **Episode & tasks** is a link to the ref's current chain
member, shown only when it has one.

Choosing a ref keeps the view and carries a chat or Auto-research episode
selection only when it belongs to the destination; a node stays selected only
when the destination's snapshot has it. A ref switch is not a project open: no
opening screen, the header and view stay mounted, and the previous ref's
snapshot stays on screen with the panel inert until the new ref's snapshot
arrives. A ref visited before restores from its `(project, ref)` tab state. The
session's target fences still drop responses for a ref already left, and each
ref keeps its own staged draft. Heartbeat single-flight is keyed by project and
ref. All project view URLs come from one helper over (project, ref, view) plus
an optional chat or episode; an exact Experiment run route keeps its own
`experiment`/`target`/`branch` fields.

## Personal owner admission

Personal API requests and terminal upgrades require an owner session. Public
personal health exposes adoption identity only. Team public health retains its
full runtime payload, including the space name and active-agent count required
by desktop bootstrap. The Web shell, auth exchange and code
redemption, and OPTIONS remain public. The separate phone listener is unchanged.
The owner cookie is host-only, HttpOnly, and SameSite=Strict; HTTPS adds Secure.
Team cookie policy remains separate.

The Web reads identity before protected boot data. A personal 401 opens the
sign-in boundary; a one-time code can be pasted or supplied in the sign-in URL's
fragment. Project locator intent stays in the URL across sign-in. The human
confirms it before registration. Display-name entry remains a separate action.
Authenticated `/api/health/details` supplies runtime and project-creation data.
The Web keeps public identity separate from authenticated details and reads
project-creation controls only after authentication.
During desktop status, an owner-session 401 clears the cached native session and
returns the verified backend identity with `owner_authenticated=false`, allowing
the ordinary sign-in boundary. Quit and update still require authenticated
health details before using active-work counts.

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
boolean. The preference exists before the chat has messages. A failed write
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

The project-scoped terminal routes are:

- `GET /api/projects/{project_id}/terminals/repositories`: repository alias,
  starting path, per-machine capability, and running Work identities.
- `GET /api/projects/{project_id}/terminals`: open sessions and live/idle state.
- `POST /api/projects/{project_id}/terminals/probe`: invalidate this project's
  machine probes and schedule fresh results.
- `POST /api/projects/{project_id}/terminals` with `repository_id`: open or return
  the single existing session for that repository and project. With
  `require_new: true` it returns 409 instead of an existing session, decided
  under the manager's lock; the voice agent uses it before typing.
- `DELETE /api/projects/{project_id}/terminals/{session_id}`: end that session.
- `WS /api/projects/{project_id}/terminals/{session_id}/ws`: binary PTY output,
  JSON `input` (`data`) and `resize` (`cols`, `rows`) messages, and a JSON `ended`
  notification.

Every HTTP route checks project membership. The WebSocket performs its own
identity, project membership, same-origin, and maintenance admission because
HTTP middleware does not cover upgrades. It rechecks before every input, on a
short interval before output, and periodically while connected; cookie
revocation and loss of membership close the connection. Output is rechecked on
an interval rather than per frame because the check is a synchronous store read
and a PTY can produce hundreds of frames a second. Unknown and nonmember projects remain indistinguishable.

Repository rows retain `eligible` and `unavailable_reason` and include
`machine_id`, `backend_id`, `backend_name`, `containment`, `os_name`,
`probe_state`, and a `reason` for every outcome. Available machines report `mirrored` or `cooperative`; unavailable
machines have null `containment`. The reason names the applicable launch
capability, missing canonical-state protection, or failed prerequisite. Remote
probe states distinguish `pending`, `reachable`, `incapable`, `unreachable`,
`authentication_failed`, and `host_key_failed`. A cold projection schedules one
probe per machine and returns pending immediately; repositories reuse the cached
result without an SSH round trip per row. The cache lives with the terminal
manager and is invalidated explicitly by Refresh or by changed machine metadata.
Space kind does not participate in eligibility. The projection and open guard use the same
per-machine capability resolution; a later mirrored launch failure remains a
hard failure with its real diagnostic, never a cooperative session.

Session payloads carry `containment` and `protection_notice`. A cooperative
session explicitly reports that canonical-state protection is unavailable on
this machine. The Terminals destination appears when any machine can host a
session, and whenever the project has an open one, because the destination is
the only way back to a running shell and the only way to end it. Remote pending
and failed probes also keep the destination visible so members can read the
reason and refresh it. Empty projects and projects with only unavailable local
machines and no open session hide it. Repository controls show unavailable rows
with their reasons. A lost SSH link produces an `ended` reason, removes the
session from the open list, and leaves the diagnostic visible without offering
to reconnect into a new shell. That diagnostic belongs to the open destination
and survives a refresh there, not a departure from it: the session projection
carries open sessions only, so a link that drops while the member is elsewhere
leaves the reason in the persisted record rather than on their screen.

The shell runs as the service account with the Work trust boundary. A mirrored
session's mount namespace provides accident resistance to mistakes; it does not
isolate a member from that account.
Per-machine capability and canonical-path refusal are owned by
[Providers and containment](providers-and-containment.md#member-terminals);
session expiry and metadata are owned by
[Projects, spaces, and operations](projects-spaces-and-operations.md#member-terminal-lifecycle).

## Since you last looked

The pull-only digest is per member and project. `GET /api/projects/{id}/digest`
returns `cursor`, `mark`, `needs_you`, `changed`, `branches`, `ran`,
`changed_node_ids`, and `count`. The count is the number of rendered lines across
those four groups. Opening a project or reading its digest never advances an
existing mark. A member without a mark starts at the event log's current cursor
and sees an empty digest; listing project cards does not create marks. The initial
main projector checkpoint is persisted, after existing branches and episodes are
baselined, before a member mark can be inserted.
Before that checkpoint, GET signals the application projector and returns an
empty digest with `mark: null` and `cursor: 0`, without creating a mark;
Caught up returns 409 and landing counts are zero. Requests never project history.

`POST /api/projects/{id}/digest/caught-up {"seq": ...}` acknowledges the cursor
that was displayed. It returns `{"mark": {"seq": ..., "marked_at": ...}}`,
never decreases the mark, and rejects with 409 a cursor ahead of the committed
event sequence or a member who has no mark yet. The sequence high-water survives project deletion, so
deleting another project cannot invalidate a cursor already displayed.
Events committed after the displayed cursor remain new. The acting member owns
the mark across devices. This acknowledgment skips project work admission but
retains global maintenance, membership, and request origin/JSON checks.
The Overview card labels `marked_at` as the time the member last caught up, shown
in the browser's time zone; for a new mark it is when the member first opened
the digest.

Needs you contains new, still-open Proposals, ready/revisit Decisions, open ask
questions, and episodes needing human action. Changed on main groups accepted
semantic changes by their attributed source. Each touched node belongs to its
latest eligible source; the viewer's direct human edits, and the Work turns the
viewer asked for in a chat once the viewer's chat read marker reaches them, are
excluded before that assignment, so an earlier agent touch remains visible. A
turn that finished after the viewer left stays in their digest until they read
that chat. Excluding a Work turn is a visibility rule only: the provider agent
remains its author. Removed nodes and
changed edge endpoints participate in grouping, while `changed_node_ids`
contains only nodes still present on main. Branch revisions aggregate into one
line per episode. Ran contains ended episodes and compute jobs, failed
non-conversation tasks, and consolidation and episode reports. Lessons and chat
messages or turns as such are excluded.

Attribution is fixed when graph events are projected: episode merge,
consolidation, identified human, ingestion, captured conversation Work, other
agent, system, then legacy unattributed human. Source keys are stable; titles
are display labels. Missing task enrichment does not discard a graph event.

SQLite owns a commit-ordered append-only event sequence and member marks.
Operational writers append events in the same transaction as their source
writes, so an older external completion timestamp cannot hide a newly recorded
job. An independent background projector follows accepted main and branch
history, catches up after missed signals, and never writes canonical state.
Its persisted per-target head detects non-prefix history after restore/reset;
that target is rebaselined with one `reset` event in the checkpoint transaction.
Shared card/count assembly ignores that target's attention entries and
`graph_change` rows at or before its latest reset; operational events remain.
It shares semantic comparison with branch changes and does not consume
notification sender state.

`GET /api/projects` includes `digest_count` on each visible project card. It uses
the same grouping and live operational filters as the digest, reading at most
`DIGEST_LANDING_EVENT_LIMIT` latest events per project. The integer count is a
floor when that window truncates the backlog; the full digest remains exact.
Neither landing counts nor digest requests replay graph history. Unmarked
projects have count zero. Digest events, marks,
and projector heads are excluded from project transfer, removed with project
control-plane data, and included in application backup/restore.

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
calling member's five toggles: `proposal`, `decision`, `blocker`,
`episode_needs_action`, and `episode_finished`. All default on except
`episode_finished`. These routes require project membership. Project Settings
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

One FastAPI backend serves the JSON API and, when built, the React/Vite
application. The optional Tauri shell starts or reuses that same backend. There
is no second team protocol or frontend-owned background-worker runtime.

`api/app.py` owns explicit composition, run dispatch, startup recovery, and
watcher runtime. Extract another control layer only for measured owner
collisions or a concrete testing problem, as recorded in the
[backend structural decision](../decisions/2026-08-20-backend-structural-refactor-closure.md).

Route handlers resolve identity and membership, validate request shape, stage
intent, and call the owning service. They never write `.research` files,
materialized output, branch metadata, or Patch history directly.

Every graph mutation response uses one strict transition projection containing
the graph target, head, graph, Experiment-control map, guidance validity,
transition/ruleset identity, primary question, graph counts, and any causal or
attention inputs from the same final state. Preview responses are explicitly
noncanonical and name their base head and ruleset. The browser renders these
published values instead of recalculating a second transition result.

Any derivation whose inputs are all backend state belongs to the projection:
for example, `EpisodeResponse.health`, `recommendation`, `live`, `can_*`, and
`ExperimentControlState.graph_reasons`. Only derivations with a UI-specific
input, such as the trust-view lens, stay client-side. `web/src/core/types.ts` is the
single response-shape restatement. Fully projected lifecycles use opaque types,
including `EpisodeStatus` and `AgentTaskStatus`, so client branching on the raw
status cannot compile.

Project snapshots and transition projections publish exact graph-attention
membership as pending Proposal ids, Decisions awaiting choice, and asserted
open Blocker ids. Pending Proposals additionally publish their ordered action
lines, including incident relations removed with a node. A field-change line
carries the node's current value as `before` and the proposed value as `text`;
the card shows it as branch diffs show a changed field. Counts are lengths of
that same projection. The browser maps those ids and action lines onto the graph
it is presenting; Inbox, Overview, and Runs never reapply the membership or
Proposal-operation predicates. A backend preview supplies both the candidate
graph and candidate membership, while a rule-inert local draft retains the
current backend membership until Sync. Cached snapshots are invalid when their
membership or the three corresponding counts disagree with their graph. The
browser validates the exact projection shape, exact Proposal-action membership,
and referenced graph member types; missing or malformed membership fails the
snapshot instead of becoming an empty attention view.

Each Experiment-control entry is also a complete read model. In addition to
budgets, reasons, episode, and operational history, it publishes health,
recommended action, Runs section, liveness, Start/Stop/report availability,
pending Stop, the exact Resume/Retry and provider-switch controls, and whether
the human has closed the Experiment node itself. The browser may translate those
closed answers into labels and layout, but a newer task poll or raw episode field
never overrides them. Runtime, episode parent,
visible tasks, budget usage, and report are read inside one SQLite snapshot, so
one response cannot splice lifecycle facts from different instants. Recovery
controls bind to the exact operation id named by the backend and disappear when
that task row is unavailable. A backend candidate graph must be synced before
Start because the run endpoint still authorizes the canonical graph; a
rule-inert local prose draft does not create that fence.

Concurrent project snapshot requests are fenced per project and graph target by start order,
including equal-revision responses. Once a newer cache, reload, watcher poll,
settings save, or Sync starts, an older response cannot overwrite its graph or
operational controls.

A completed project snapshot publishes whether canonical graph mutation is
available and the exact unavailable reason. Watcher responses similarly publish
whether **Check now** is currently available. These are backend decisions, not
client reconstructions from replay, status, or notification fields.

Every branch route proves the branch belongs to the requested project and
episode. Branch lists, targets, indexes, Research, Inbox, Chats, and merge
eligibility reads accept both Experiment and Auto-research owners. A branch id
alone never grants lookup. Task, watcher, episode, and Experiment detail APIs
preserve exact `main` versus `branch:<id>` target identity.

Graph, snapshot, history, Sync/preview, and ordinary chat/task routes accept an
optional `branch_id` query naming an existing episode branch. Omitting it selects
main; project-wide task and watcher lists retain their project-wide default.
Snapshots publish `graph_target`, `graph_head`, and `graph_changes` (null on main).
`graph_head` is the exact head, transition id included, that Sync/preview builds
on. Display caches written before it named the id carry null there; the Web keeps
the head it already observed at that revision.
`graph/changes?branch_id=...` publishes the same canonical base-to-head semantic
delta, changed and neighboring node ids, before/after values, and Patch/task
provenance. The backend derives that read model from one coherent branch replay.
A project tab reopened from the space page or another tab returns to the graph
target it was last left on, main or branch.

The durable project display cache remains main-only. A branch cached-snapshot
request returns an explicit cache miss, and the authoritative snapshot endpoint
opens the exact branch. Target-scoped polling observes its branch revision and
graph mutation availability, including merge-state changes at the same revision.
Branch reads validate refreshed state without repairing or publishing graph
outputs; an unavailable canonical refresh fails explicitly.
Neither a branch response nor a delayed main response may replace the other
target's state. Experiment controls and graph-wake evaluation use that same
target. An active branch merge publishes graph mutation as unavailable and
rejects manual Sync and new graph writers until it settles. An owner merge
reservation also fences new Discuss turns on that binding through cleanup.

## Human question API and cards

Project members read questions through
`GET /api/projects/{project_id}/chats/{chat_id}/questions` and
`GET /api/projects/{project_id}/episodes/{episode_id}/questions`. Episode lists
include predecessor questions without changing their origin. Each question
publishes its id, owner and asking operation, origin capability, text, choices,
multiple-selection flag, state, answer, chosen choices, human resolver and time,
creation time, `withdrawn_readonly`, the server's `can_answer` offer, and
`agent_waiting`. The latter is true for an open, non-withdrawn question whose last
pending ask round arrived less than 10 seconds ago, using the server's monotonic
clock. This process-local freshness tolerates the remote mailbox polling cadence.
Open questions whose asking turn has settled, and orchestrator questions, project
as `parked`.

`POST /api/projects/{project_id}/questions/{question_id}/answer` accepts only
`answer` text and `choices`; the store validates both and records the
authenticated human. An exact retry is idempotent; a conflicting resolution or
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

Node and project chats show open question cards above the composer and resolved
cards read-only in transcript order. The composer retains its steering behavior.
A single choice submits immediately; multiple choices use toggles and explicit
submission. Free text remains available with either choice format. A parked
chat answer with no running turn names Discuss or Work continuation on its submit
control, matching the asking turn's capability.
Experiment and Auto-research detail show the same cards; withdrawn cards retain
their history with an **Episode ended** state. Question refresh follows existing
chat and episode refresh/polling, including turn command/state changes and the
active project heartbeat. Fresh pending rounds show a cobalt **Agent is waiting**
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
Episode responses include the owner's nullable `isolation_state`. It carries
the reservation, attempt phase, recorded cleanup steps and errors, delivered
code commit and target, squash commit, and graph archive flag. Graph receipts
remain the independent graph delivery record.

`GET /api/projects/{project_id}/episodes/{episode_id}/merge-preview` accepts an
optional `target_branch`. It recomputes `MergePreview`; no preview is stored.
The response contains `delivered_baseline`, graph operation count, residue
paths with reasons, per-path `paths`, optional code status, and `needs_agent`.
Each path names its entity, id, and field path, its base, branch, and main
values, and the builder's `delivered`, `conflict`, `needs_agent`,
`needs_proposal`, and `residue_reason`, from the same builder call as the
residue. `needs_agent` also covers a code conflict. Code reports its
repository alias, source and target branches, commits ahead, leftovers, and
conflict files. Its status is `clean`, `conflict`, `already_merged`,
`target_dirty`, or `target_missing`.

`POST /api/projects/{project_id}/episodes/{episode_id}/cleanup` accepts the
three cleanup choices and `confirm_discard`. Unmerged removal or graph archive
requires that confirmation. Each step is recorded and retryable. Graph archive
only hides the branch from the default episode list. The list query
`include_archived_branches=true` includes it again. Explicit episode and branch
reads remain available, and Patch history is unchanged.

## Compute setup and job APIs

Project machine entries expose the manifest `compute` block and live
`compute_probes`, one `ComputeBackendProbe` or null per route. The latter is
read from storage even when the graph snapshot is cached. Settings updates
accept `machine_compute`, a partial alias-to-`MachineComputeConfig` map; null
removes a block, omission preserves it. Validation and TOML persistence belong
to the machine configuration owner. A changed block invalidates its stored
probes and schedules a fresh background check of that machine.
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

Settings stages per-machine `job_manager` and optional helper `jobs_root` beside
provider paths. **Use Slurm** opts into direct scheduler submission; RCP exposes
no resource-argument inputs. Only changed aliases are saved. Draft storage keeps
explicit machine edits, so an unrelated draft cannot restore an older full
compute configuration. Unsaved machine edits mask readiness. A save that
changes a machine's block probes its offered routes in the background; Runs
offers **Check again** through `POST .../machines/{machine_alias}/compute/check`.
Each stored probe supplies its own label, tone, and diagnostic.

Chat and Experiment use the existing watcher refreshes to show one external job
row. It displays the log path, observation state, check diagnostic, and Cancel
history. Human Cancel follows the backend's capability even for a stopped
watcher, independent of graph read-only mode; the API enforces project write
admission. The immediate action response updates its row without adding a
second job-list polling lifecycle.

## Atomic client project snapshots

The Web client stores a bounded project snapshot keyed by project id and exact
head. A successful mutation atomically replaces graph, control, guidance,
transition manifest, and derived inputs. The client never overlays a new graph
on an old control map and never implements transition rules.

Human edits that the backend manifest proves rule-inert may update a local draft
immediately. A possible trigger, absent manifest, or stale tag previews through
the backend. A preview conflict retains the invalid edit and last valid draft
separately. Sync revalidates the complete batch against live canonical main and
commits once or not at all.

Human Sync accepts built-in and active custom nodes through the existing
`custom_nodes` collection, plus `added_edges` and `removed_edge_ids`. Preview and
commit share the same backend owner and exact base revision. Edge replacements
remove the old edge before creating its replacement in the same transition;
layers are resolved by the backend. The membership-protected `graph-edit-options`
endpoint publishes built-in relation names, Evidence-assessment requirements
and node ID prefixes from backend rules without replaying project history. The
client reads custom relations from the displayed ontology; backend preview completes
new node defaults and resolves edge layers. A draft with edge
changes retains its original edge-edit revision across refresh/reload; it cannot
silently rebase connection edits onto a newer graph.
These controls remain human-only and add no WebMCP graph-writing tool.

Resolved/superseded Blockers remain canonical but are omitted from active
Research-flow and attention projections. Stale Experiment summaries and next
actions are labelled historical and never rendered as current guidance.

## Revision observation and drafts

Visible clients notice canonical main changes without browser reload or
repurposing the Seed/Refresh action. Every open project tab sends a cache-only
heartbeat on the bounded visible cadence; the active tab observes completed
cached revision updates more frequently, and visibility resume sweeps all tabs.

A heartbeat may schedule one bounded lock-free, single-flight remote-head probe
per project. A temporarily unavailable head does not replay or copy the graph.
An unchanged head also skips that work unless the cached canonical state is
offline: a successful probe then triggers authoritative reconciliation before
clearing the offline state. Movement starts the same background reconciliation;
an older result may not replace a newer cache.

Reconciliation preserves human drafts. A staged node whose canonical revision
did not move stays committable. One that moved becomes behind and is excluded
from Sync until the human edits it or reversibly swaps an incoming field into
the draft. Paper drafts use their own equivalent whole-document rule. Display
caches never become canonical input or graph authority.

## Project index and identity

The project index keeps project cards first and one distinct space-level
**Runs** ledger below. It aggregates the current Experiment-loop parent selected
by each Experiment's backend control with Auto-research parents across visible
projects. **Needs Action** and **In progress** each stay unfolded and mix both
modes in reverse chronological order, under the same truthful split the project
ledger uses; **Completed** folds by Experiment loop then Auto-research.
The backend owns membership, lifecycle placement, health, project identity, and
the exact Experiment route. The space ledger keeps completed parents for seven
days, without deleting episode records or changing project Runs or History.
Auto-research rows carry their exact episode identity into project Runs, so a
completed or non-leading row opens that parent rather than the default card.
That exact parent is read independently when it falls outside the project's
bounded episode list.

The index header contains the compact current-human identity control. An unnamed
personal owner sees **Sign in** as naming the durable local identity, not creating
an account. The panel shows editable display name and exact read-only copyable
user id. A team member uses the server login/session boundary and can manage
their own credential. Pending project invitations appear on the index.

The team identity panel includes **Devices**, listing the member's unexpired
sessions with connection and last-seen times. **Current device** has no Revoke
control; ending it remains Logout. The panel refreshes on opening and after a
successful revoke, and offers an explicit Refresh action.

**Connect a device** in that panel issues a pairing code through
`POST /api/team/devices/pairings` and shows it once, with its expiry, until the
member dismisses it or the code ends. A team may carry an **access address**, the
https origin members open on their own devices (typically the tailnet front in
front of the server). It is operator-set in the installed server configuration
(`access_url` in `/etc/rcp/team.toml`, or `RCP_TEAM_ACCESS_URL` for a
source-run server) and read-only to members: `GET /api/team/space` returns it
with the team name, `PATCH /api/team/space` changes only `name`, and doctor
reports it as `team_access_url`. When the address is set, the issued code carries
`connect_url = <access_url>/#pair=<code>` and the card draws it as a QR code
beside the code; a phone that scans it lands on the login screen with the code
filled in. Without an address the card says how to set one. While the code is
visible the panel polls
`GET /api/team/devices/pairings/{pairing_id}`, whose `status` is `waiting`,
`consumed`, `expired`, `revoked`, or `locked`, and refreshes Devices on
`consumed`. A code is bound to the session that issued it: when that session is
revoked, logged out, expired, or removed by credential rotation, the code reads
`revoked` and cannot be redeemed. A member holds one live code: issuing another
withdraws the previous unused one. Codes are
ten characters from an alphabet without I, O, 0, or 1, shown as `ABCD-EFGHJK`,
expire after ten minutes, are single use, and lock after five wrong secrets like
enrollment codes; the server stores only the hash of the secret.

The unauthenticated team login boundary offers device pairing first: a **Device
code** field and a required **Name this device** field, submitted to the public
`POST /api/team/devices/pair`, which creates an ordinary session carrying that
label and sets the same cookie as exchange. A second mode, **Sign in with a team
token instead**, keeps the credential slip. The pairing route never sees or
returns the member token, needs no team-shell protocol declaration, and shares
the public-body size bound with enrollment and exchange. Its failures map to
401 (invalid), 409 (used), 410 (expired), and 429 (locked).

`POST /api/team/session/exchange` accepts an optional human-authored `label`
string of at most 80 characters. Omission stores `Unnamed device`; migration 14
also assigns that literal to existing sessions. The label is stored and rendered
as untrusted display text, without normalization or inference from request
fingerprints. Supplying a label remains optional for every supported native
protocol version. There is no rename route or label-entry screen in this change.

`GET /api/team/sessions` returns only the acting member's unexpired sessions.
Each entry exports `session_id`, `label`, `created_at`, `last_seen_at`, `expires_at`,
`is_current`, and `can_revoke`. The backend computes both decisions and reports
the current authenticated session as not revocable. Public identifiers are
independent random UUIDs, never session tokens, hashes, or derivatives of either.
Listing does not refresh other sessions' idle expiry.
The source-built desktop holds one session per saved connection. It keeps the
exchanged session's secret (the cookie value alone; Apple's Keychain tool keeps
only 128 prompt characters, so the whole Set-Cookie line does not survive) in
the Keychain beside the member token, rebuilds the cookie from it, verifies it
against `/api/identity` at launch, at Reconnect, and before each native request,
and exchanges a new session only when the server answers 401. One desktop is
therefore one row in Devices across launches. Forgetting the connection on the
desktop discards the saved session locally; its server row idles out or is
revoked from another device.

`POST /api/team/sessions/{session_id}/revoke` deletes that member's named session
and returns `{"ok": true}`. Unknown, expired, and other members' identifiers all
return the same 404; the current session returns 409 directing the member to
Logout. Other sessions and the member credential remain usable. Both routes
inherit team authentication and mutation-origin enforcement, and return 404 in
a personal space. A personal identity panel's **Devices** section lists
notification targets instead: **This Mac** and notify-only phones, which have
Remove rather than Revoke, and **Connect a phone**.

In a team space each session row shows its notification state (on, off, or
Delivery failed). Only the current device's row has **Turn on**, **Turn off**,
and **Test**; in the Mac app it drives native notifications, and in a browser it
subscribes to Web Push, asking for permission only from that tap. A browser
without Web Push capability is told to add RCP to its Home Screen. Each
signed-in browser visit re-registers a live subscription, keeping its device,
and removes a server device whose browser subscription is gone.

Projects hidden by membership never appear as locked cards. Losing access
closes its open tab and returns to the index.

## Confirmed team desktop target

The first team client is the source-built desktop app. Its local project
index groups the personal space and saved team connections without making the
local backend an authority for any team project. Each connection stores
nonsecret routing metadata, its expected `space_id`, compatibility information,
and bounded last-known project cards. The permanent member token stays in the
operating system credential store and is absent from URLs, page storage, saved
connection JSON, logs, and Tauri command output.

**Add team space** first establishes the SSH tunnel and verifies the nonsecret
space identity. A new member enters a bootstrap/invitation code and display name;
an existing member may enter their permanent token. The controlled secret field
and IPC request are cleared after the one enrollment/storage operation. A newly
issued permanent token is captured by the native shell and written directly to
the credential store. No secret becomes local-backend state or cached connection
metadata.

The native shell uses the system SSH configuration and agent to hold a loopback
tunnel to the team server. Before establishing a browser session it checks
health and the expected `space_id`, then selects the highest overlap between its
compiled inclusive team-shell protocol range and the server's advertised range.
It sends that selected integer on enrollment, permanent-token exchange, and the
bounded project-card read and requires the server to echo it exactly before it
installs the HTTP-only cookie. A missing range, no overlap, or a missing or
different echo refuses the connection. The error names the observed desktop and
server source commits and tells the member which side to update or rebuild from
current `origin/main`; Git commit ordering and application semantic versions are
not compatibility protocols. A changed `space_id` likewise blocks mutations
until the human explicitly reconnects. An unavailable or incompatible team
connection leaves personal work usable and shows its cached cards as
unavailable.

The current desktop range is `[3, 4]` and server range is `[1, 4]`. The desktop
selects `4` with a current server, keeps protocol-3 connections, and refuses
servers below protocol 3 before enrollment or transfer. Updated servers still
accept older protocol-1/2/3 clients.
Protocol 1 keeps team
project cards non-deletable. Protocol 2 adds team deletion: cards may advertise
`can_delete=true` together with the exact `delete_confirmation`, and a team DELETE
is refused with the protocol-mismatch response unless the caller selected 2 or newer.
Protocol 3 preserves that contract and adds native transfer relay support for
optional reviewed `source_commit` fields and `rcp-transfer-v2` Git archives;
legacy omitted-commit requests and v1 archives remain accepted. Protocol 4 adds
the optional `record_schema_version=2` source-configuration field so episode
archives travel with operational history. Archive-free transfers retain the
previous configuration and record bytes. Older clients or targets that cannot
decode the extension refuse preparation before source release. A breaking
change adds a new immutable per-version contract before either end advertises
it. Narrowing a range is explicit retirement, not an automatic current-plus-
previous or time-based rule. The native handshake ends after
health/source/space identity, enrollment or token exchange, returned member
identity, bounded project cards, and HTTP-only browser-cookie installation. It
does not include server operations, provisioning policy, provider work, or the
ordinary server-served Web/API surface. Native transfer relay wire compatibility
is versioned here because the desktop, not the server-served Web app, sends it;
this is not an operator-capability or per-feature discovery registry.

Compatibility is negotiated live and is never durable connection authority.
Registry version 3 removes the shipped `minimum_shell_version` field through an
automatic version-2 migration while preserving connection identity, SSH target,
local origin, expected space, cached cards, operator route, and the independent
Keychain credential reference. Unknown registry versions or fields still fail
closed, except inside cached project cards. Cards are display data: the desktop
keeps each card's id, name, and attention count as the server sent them, ignores
other card fields, and drops cards to fit the registry size limit. Server text
never fails a connection. Sign-in responses likewise accept added fields; only
identities and protocol are checked.

HTML pages from the Web mount carry `Cache-Control: no-cache`, so every load
revalidates by ETag and a server update reaches the next team-space entry or
reload. Without it WebKit kept an old page fresh for a tenth of its age, hours
after an update. Hashed `/assets` files keep default caching.

Each space serves its own project index, so leaving a project returns to the
index of the space that project is in, by the same control and shortcut in
both. Leaving the space is a separate explicit action: the team index names the
active space and carries the one **Exit team space** control, which reloads the
local backend. The local index is still the only screen that shows more than
one space at a time; the identity record names the saved team spaces without
becoming a second way into them.

For a saved connection, the macOS shell launches `/usr/bin/ssh` directly with
argument-vector execution, batch authentication, no remote command or terminal,
exit-on-forward-failure, bounded connect/readiness timers, keepalives, disabled
control sharing/backgrounding, and one explicit
`127.0.0.1:<ephemeral>:127.0.0.1:<configured-server-port>` forward. It therefore
uses the member's existing SSH config and agent for host, key, proxy, and host
verification without accepting an interactive password prompt inside RCP. A
desktop-owned dual-stack loopback TLS listener presents the desktop identity at
the saved HTTPS origin and forwards plain bytes only to that private SSH
listener. One healthy connection is reused only while origin, SSH target, and
remote port still match; an ended, changed, or failed connection observes a
short backoff before replacement.

The team backend's mutation-origin check recognizes that one exact boundary: a
request received over the private HTTP side of the tunnel may carry the matching
generated `https://rcp-<connection-id>.rcp.localhost:<port>` origin. It does not
accept any other HTTP-to-HTTPS origin substitution. This keeps browser mutation
protection aligned with the desktop TLS terminator instead of rejecting the
desktop's own invitation and team-control requests.

A personal backend listens on a predictable loopback port, so any web page can
aim requests at it, and its owner cookie is the session those requests would ride. Browsers send a cross-site POST
without a CORS preflight only when it has no content type or a simple one
(`application/x-www-form-urlencoded`, `multipart/form-data`, `text/plain`).
The personal backend refuses those POSTs with 415
`personal_simple_request_refused`. The one exception is the multipart chat
attachment upload, matching the team rule. PUT, PATCH, and DELETE always
preflight. Every RCP client already sends `application/json`, or
`application/octet-stream` for transfer proofs.

Tunnel admission owns saved-row lookup and closes before removal, Quit, or
desktop update drains children. A failed lifecycle operation explicitly reopens
admission only when the app returns to service. A cleanup failure keeps the
unconfirmed child record for retry while its supervisor remains live and for
diagnostics otherwise, instead of forgetting ownership. The personal origin may
connect any saved row, while a team origin may reconnect only its own exact row.
A team page whose request never reaches its backend (the SSH tunnel ended, for
example across sleep) uses that right itself: one recovery at a time calls the
native Reconnect with a capped backoff until it succeeds, then reverifies,
never replaces, the accepted backend identity and reloads the active project or
project index, so the page recovers in place and a changed backend still stops it. None of these operations signal or restart the
remote RCP service.

A failed backend check never unmounts what the page already shows for that
backend. While the check fails, and while the identity read that follows
recovery runs, a blocking reconnect overlay covers the rendered project or
index, so scroll, open panels, and unsent text survive. Recovery that confirms
the same backend (version, instance, data-directory identity, and space)
reconciles the open project with an ordinary reload, never a fresh open. A
check that observes a changed backend replaces the page with the reconnect
screen, and the project opens from scratch after the human reconnects.

Every saved space receives a stable, distinct loopback origin. Different ports
on the same `127.0.0.1` host are not isolation because cookies ignore ports; such
tunnels would collide on the shared `__Host-` session-cookie name. The shell
uses
`https://rcp-<connection-id-without-hyphens>.rcp.localhost:<local-port>`.
Navigation admits only exact origins derived from its validated saved-connection
registry. The desktop stores one versioned authenticated-encrypted
certificate/private-key file and keeps only its 32-byte sealing key in the
operating-system credential store. Source builds access that key through
Apple's signed Keychain tool with an ACL pinned to that tool, not an ad-hoc app
cdhash; the value never enters argv, URLs, page state, logs, or native command
output. The desktop pins the certificate in the live WKWebView navigation
delegate and never installs system-wide trust. A malformed, unauthenticated,
unknown-version, or partial identity/key pair fails startup rather than being
silently replaced. The current version-4 identity record stores its expiration,
uses a 365-day explicit non-CA leaf for `*.rcp.localhost` with TLS server-auth
and key-usage extensions, and rotates atomically during the final seven days.
Valid earlier identity records are reissued through the authenticated identity
owner, while the exact previously shipped `rcp-<id>.localhost` registry origin
migrates without changing the saved team connection or its member credential.
The macOS bundle makes the narrow ATS exception required for manual server-trust
evaluation only for `rcp.localhost` and its direct subdomains. This does not
permit HTTP application navigation: the proxy speaks TLS, navigation still
requires one exact saved HTTPS origin, and the native handler accepts only the
pinned leaf after hostname, validity, and server-use evaluation.
Each team's TLS listener closes any connection whose SNI is not that team's
alias before opening its upstream, so a shared wildcard leaf never routes one
team's host-bound cookie to another team's server.
The tool ACL solves source-rebuild stability; because a same-UID process can
invoke the same general-purpose Apple tool, it is not same-account read
isolation. That limitation is accepted under the current cooperative provider
model for source builds and for the unsigned prebuilt app alike: an unsigned
app has no stable signing identity to bind access to, and neither kind of build
is more exposed than the other. App-bound credential access is later work.
Team tokens use the versioned service
`app.researchcontrolpanel.rcp.team-member-token.source-v1` and a distinct
connection account. The immediately preceding D4 checkpoint never created a
saved team registry or a token in its unversioned pre-live namespace, so this
source-mode namespace break has no credential to migrate.
The Tauri capability covers the bounded hostname family, but the saved-origin
navigation check and certificate pin remain independent fences. The production
boundary and its real-WKWebView evidence are recorded in the
[local HTTPS decision](../decisions/2026-08-30-desktop-local-https-origins.md).

The ordinary browser can use the team server UI when transport already exists,
but it cannot own multi-space routing, credential storage, SSH tunnels, or
server-command execution, so it does not advertise the native **Add team space**
action. A saved connection that cannot establish a session stays visible with
its bounded cached cards, the native failure diagnostic, and a reconnect action;
it never becomes an apparently empty or local space. Source mode is the
supported client for this slice; a packaged Linux client is not required.

Project creation and transfer use one visible project wizard. Its three named
intents are **Use an existing checkout personally**, **Create a shared team
project**, and **Move an existing personal project to a team**. An entry point
may preselect an intent, and both the personal project card menu and Project
Settings deep-link to the move intent, but the user does not encounter separate
personal, provisioning, and transfer wizards. Each backend exports only its own
product eligibility, preselection, required fields, and any pinned source
identity. The desktop-native bridge
separately exports relay capability and its authenticated saved team targets.
The wizard offers move only by intersecting explicit permission from the
personal backend, explicit admission from the selected team backend, and that
native capability answer. A browser has no native capability answer and cannot
offer cross-space move. The Web does not infer product authority from
`space_kind`, paths, saved-connection presence alone, or native-global
detection.

The modes retain their separate authority owners behind that shared surface.
Personal setup calls the ordinary path-based preflight/finalizer. Team creation
creates a backend-owned durable provisioning request with derived central paths.
Each repository accepts an optional GitHub `source`; omitted, null, or blank
means server only. The repository truth choice `count_as_project_truth` defaults
to true. The response exposes `source_kind` (`github` or `server_only`), a
nullable repository identity and GitHub URLs, and the truth choice. Request kinds
also include `add_repository` and `connect_repository`, whose
`target_project_id` identifies the existing project. Reading those requests
requires current membership in that project.
`POST /api/projects/{project_id}/repository-requests` accepts either
`{"kind":"add_repository","repository":{"alias","source"?,"machine_alias","count_as_project_truth"}}`
or `{"kind":"connect_repository","alias","source"}` and returns the ordinary
provisioning response. Placement uses an existing project machine. Setup runs
through `rcp server project provision <request-id>`. The ordinary completion
endpoint accepts the review digest; for repository requests only the requesting
current project member may confirm. Add applies manifest membership and the truth
choice through a human approval transition, bound to the captured manifest/head.
Connect preserves the manifest. Server-only setup and both repository request
kinds expose the same setup and review controls as team creation.
Native provisioning and transfer parsing preserve nullable sources. The native
relay sends no GitHub URL for a server-only repository and requires its reviewed
commit and Git bundle; GitHub repositories keep their optional bundle choice.
Personal-to-team transfer creates linked requests
in the two authenticated backends and is available only in the
source-built desktop because its native shell owns the archive relay. A direct
team request to `/api/projects`, `/api/project-setup/preflight`,
`/api/project-setup/ssh-paths`, or `/api/project-setup/create` is refused before
request-body interpretation,
filesystem inspection, or catalog mutation. The separately validated
provisioning finalizer is the only team-project entrance into the existing
setup/registration owners.

The personal public backend health projection carries adoption identity. Team
public health keeps the full runtime and project-creation projection for its
existing bootstrap consumers. The protected health details projection carries
runtime fields in both spaces. `version` remains the full
package `__version__` verbatim, and
`running_commit` keeps its existing meaning as the commit recorded for the
running installed process. It additionally publishes `build` as the integer
package build number or null for a source checkout, `commit` as the package
build's 7-to-40-character hexadecimal commit or null. Protected details expose
`schema_ledger_head` as the newest applied storage-migration number read from
the data directory's ledger.

The protected health details projection also carries one `project_creation`
control with all three
intent identities, per-intent eligibility and preselection, primary action
label, required fields, pinned source identity when one exists, and an explicit
unavailable reason. The durable provisioning response similarly publishes the
status and check labels, exact next action, `can_run_setup`, `can_review`,
`can_cancel`, canonical repository URLs, intended and resolved paths, readiness
counts, structured operator action, safe CLI argv, and final-review binding. The
Web seals the complete provisioning and check-status vocabularies and consumes
those answers instead of rebuilding lifecycle policy from strings.

A provider-check projection also carries the nonsecret proof read back from the
server: resolved executable path, provider version, durable runtime id, observed
execution account, and check time. Those fields are present only for a ready
check; credentials and provider-home paths are never response fields. The Web
renders or relays this backend evidence and does not reproduce provider version,
model, runtime, authentication, or OS-account decisions.

The UI renders the backend's status, diagnostic, exact next action, resolved
paths, and final review. It cannot claim success from a desktop subprocess exit
code. The new-team wizard leaves the GitHub URL optional: blank means server only.
The draft ledger and repository review identify server-only repositories, with
one plain notice that their code is not backed up. Setup announces the empty
RCP first commit on main, including its push for an empty GitHub repository.
RCP does not collect GitHub user authentication.

Team Project Settings offers one-repository add and connect requests through
`POST /api/projects/{project_id}/repository-requests`. Add takes an alias,
optional GitHub source, machine, and a checked-by-default project-truth choice.
Connect appears only on a server-only repository with `can_connect` from the
Settings projection. Both open the existing request-id provisioning view, with
its server command, polling, and explicit final review; submitting Settings
never confirms the request. Backend execution and authority remain with their
provisioning owners.

Transfer setup does not choose target URLs. A repository with a GitHub origin
keeps that identity on the team server; one without an origin transfers as
server only, which the review shows, and its Git bundle is required.

A saved member connection and an operator route are distinct capabilities even
when they use the same SSH host. The source-built desktop stores the latter as
nonsecret native metadata: either an explicit direct `rcp@host` target or one
named operator target using `sudo -n -u rcp -H`. The Web derives which from the
target's login, so the human enters only the SSH target. **Run setup now** appears only
after a native read-only probe proves that exact route can invoke the fixed `rcp
server project provision <request-id> --machine-readable` command. The shell
passes a validated request id as an argument and never executes arbitrary
command text returned by a server. It bounds the structured events, checks that
they come from the expected fixed command, and screens them for credentials. The
server owns their step order and text. Success comes only from an authenticated
durable request readback from the expected team space. If SSH or `sudo` needs interaction, the
app shows or opens the same fixed command in Terminal; it never collects a
private key or privilege password. The browser shows a copyable operator command
instead, written as `sudo -u rcp -H …` so it runs as pasted from the operator's
own login on the server.

CLI structured progress is presentation input only. The CLI reports each state
change to the lock-owning backend through its private local control channel, and
the Web UI refreshes the durable request. Only the final explicit human review
may create or re-home the project. For personal-to-team transfer, one desktop
review action calls both already-authenticated backends in a fixed order: target
admission first, then source release. Each backend records its own human actor;
the native relay and remote CLI cannot provide either confirmation. A partial
first confirmation is durable state that the same request resumes, not evidence
that the project moved.

The native coordinator restores the exact saved team tunnel and member session
before every cold-resume read or transfer operation; Web code never reconstructs
that credentialed state. A relay command's nonzero exit or incomplete proof and
cleanup result remains a durable retryable failure and is shown as an error. It
may expose the explicit protected manual relay, but it never silently selects
that path or labels the native call successful.

Every machine-readable operator-action event carries the same structured
responsibility, typed machine or external-service target, ordered safe commands
or GitHub actions, nonsecret values, success check, and resume command that the
interactive CLI prints. The wizard renders those fields directly and never
parses CLI prose. No machine step or recovery instruction exists only in the
wizard.

## Project tabs

The project shell keeps a session-scoped dock beside the index control. Opening
a project appends and activates one tab; reopening activates without duplicating
or reordering. Inactive tabs shrink within the capped dock while the active one
retains more width. Tabs cannot be reordered.

Each tab retains bounded in-session view, panel, scroll, selection, draft, and
exact Runs-route state. Activating a tab is cache-only and never blocks on remote
I/O. Closing a tab changes no canonical state, task, draft, conversation, or
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

When the browser host supplies `document.modelContext.registerTool`, RCP
registers a page-scoped WebMCP surface over its existing application owners. A
ready project index exposes project listing and exact project navigation. A
loaded project replaces those tools with project overview and node inspection,
each provider's sign-in state as Settings shows it,
in-page navigation, artifact/report listing and visual opening, conversation
listing, inspection, and Send, bounded Experiment inspection and Start,
Auto-research authorization, and graceful Stop of an Experiment or
Auto-research episode. In-page navigation opens an exact node, conversation,
run, or artifact, or a project tab by its visible name. The overview lists the
configured agent for each role and the ids of stoppable Auto-research
episodes, which have no Experiment node to inspect. Login, project setup,
loading, and invalid project states expose no tools; the project surface waits for the same verified backend
identity, actor, and team-session state as the index, so a reconnect screen
or overlay retires it.

The inventory follows current backend and browser state. Experiment Start,
Auto-research authorization, and Stop are registered only while at least one
exact action can succeed or while that action's accepted call is returning.
Changing state updates stable tool proxies without aborting an in-flight call;
leaving the surface unregisters its tools. Project navigation returns before
the index registration is retired, so the host does not mistake successful
navigation for a stale tool failure.

The registered tools come from one host-independent catalog
(`web/src/voice/toolCatalog.ts`) that works without a WebMCP host. `catalog()` is a
fixed list of every tool's name, description, input schema, and `confirm(args)`
predicate, whatever the page state. `confirm` is true only for a Work Send,
Experiment Start, Auto-research authorization, and a terminal command; it stays
local, so WebMCP host agents get no RCP confirmation. `catalogAsFunctionTools()`
is the one serializer to the Responses function format and leaves `confirm` and
other local markers out. `resolve(name)` returns the executable definition App
last published from page state, or a refusal that says why: the wrong surface,
nothing to stop, or the Start or authorization refusal.

The two terminal tools are voice-only. App publishes them for voice, but
`webMcpHostDefinitions` keeps them out of WebMCP registration. A shell command
needs the member's tap, and a WebMCP host has no RCP card to show.

Every call accepts exact ids returned by an RCP read tool and revalidates them
against the current page snapshot before acting. Named read results are bounded
JSON, not generated summaries, and omit storage locators. The broad reader
below returns route responses as the backend sends them, paths included. Node inspection shortens
oversized saved text, lists, object entry counts, and key names to fit its budget
and names every shortened path, so exact content is never confused with
truncated content. Conversation listing
exposes the saved chat ids the page has loaded together with the backend total.
When a conversation's turns have aged out of the bounded task window the page
holds, inspection and Send read the exact task behind its latest transcript
message, so a conversation can continue only on its original graph target.
An ordinary branch conversation can use Discuss and Work from that branch's
workspace; a main workspace cannot resume its branch-bound session.
Artifact and report listing covers both places an artifact lives: the recent
task and episode windows the page holds (it reports both window sizes) and the
Artifacts panel inventory (`GET /api/projects/{id}/artifacts`). A panel entry
the windows already show marks that record `in_artifacts_panel` instead of
repeating it; a panel-only entry gets a `saved:` viewer id that resolves
against the panel inventory, whatever the windows later hold. Node, task, chat,
and episode filters match panel entries by their source fields, so a target
whose tasks are older than the window still lists its kept outputs. An exact
`task_id`, task viewer id, or `episode_id` outside the windows is fetched from
the existing task or episodes route rather than reported missing. Artifact
opening uses the existing backend viewer inside the page after confirming
current availability. A PDF opens in the desktop's system viewer through the
same command the panel uses. In a browser, a PDF or a download-only file is
refused with where to tap Download; page agents never download.

`rcp_get_playbook` returns RCP's plain-language playbook
(`web/src/webmcp/playbook.ts`): what RCP is, how a request is routed, where
artifacts live, and what only the member may do. It names no tools. Voice sends
the same text with its session request.

`rcp_list_read_routes` and `rcp_read` (`web/src/webmcp/reads.ts`) give voice and
WebMCP one broad reader over the open project's GET routes. Reads are broad and
writes stay named and card-gated. Voice gets no page-driving or browser tool:
it could press Confirm itself and would send the screen to OpenAI. One code-owned
policy drives both discovery and enforcement: discovery is the backend OpenAPI
schema intersected with it. It admits every GET under
`/api/projects/{open project}`, root included, except downloads, redirect-only
routes, GETs with side effects (terminal reconciliation, Experiment stop
settlement, digest marks, project reconciliation, merge-preview Git writes,
the conversation worktree route's remote Git contact, the machine browser
readiness probe, and live artifact snapshots, which read remote files over
SSH), the `refresh`
query flag, which reruns probes, and the chat `inventory` flag, which would list
every graph's chats as the displayed one's. Repository file reads refuse any path whose
component matches the credential denylist (for example `.env*`, `*.pem`, `.git`,
`.ssh`, `id_rsa*`). Requests are built from admitted templates with validated
parameters; the normalized URL must stay same-origin and in the open project.
Redirects, SSE, and binary bodies are refused; bodies are read incrementally to
a byte cap within one timeout, and identity or space loss cancels the read. A
route with a `branch_id` parameter gets the displayed branch; `/history` needs
a bounded revision window. Results are paged to the tool result cap and marked
untrusted; voice receives them in an untrusted-data envelope that names the
source tool and arguments.

Newly discovered task artifacts read from RCP version storage. `kept_at` marks
Keep even when `kept_filename` is null; that filename remains legacy metadata.
Legacy result-view URLs redirect to artifact routes. An artifact's availability
is independent of its native session and stage; those still gate commenting.

Task artifacts project a `view` (`html`, `image`, `markdown`, `text`, `pdf`, or
`file`) alongside the independent `can_open`, `can_download`, `can_keep`, and
`can_discuss` capabilities. `result.artifact_omissions` carries known reason
counts and `discovery_failed`; transcript reconstruction and history
reconciliation preserve it even for a turn with no cards or answer. Saved
artifacts also project `view`, `available`, `can_download`, and a nullable
`download_url`. `can_open` always means an RCP viewer, including in the desktop
app; the system PDF action separately requires `view` of `pdf` and
`can_download`.

Conversation Send starts one asynchronous ordinary Discuss or Work turn through
the same provider profile, native-session, skill, task-admission, and local or
SSH execution path as the visible composer; it returns the durable task id rather
than waiting for provider completion. Attachments, project references, and
compute selection remain visible-composer controls. Experiment Start and Stop likewise reuse the existing
backend projections and action owners, including staged-Sync, readiness, budget,
single-start, exact-episode, and graceful-Stop fences.

Auto-research authorization takes the visible form's inputs: an
`invocation_ceiling` of any integer of at least 1, an optional
`starting_instruction`, and `code_worktree`. It calls the same start function
and refusal check as the header button, so the tool and the button refuse in
the same states. Stop takes an exact `episode_id`. With `experiment_id` it uses
the Experiment Stop route; without it the episode must be a stoppable
Auto-research episode, stopped through `POST .../episodes/{id}/stop`.

`rcp_open_view({kind, id})` shows one exact node, conversation, run, or
artifact viewer, or the Inbox, through the page's own owners. A run id is an
episode id, opened through the same exact route as an episode notification: its
Auto-research route or its Experiment's Runs entry. A conversation whose
first turn still runs has no saved transcript yet; it opens from its task, as
the Agents board opens it. The view stays in the
current project and graph target. It checks that the page still shows them
before opening and never switches project or branch; only `rcp_open_project`
changes project.

WebMCP is not a second API or authority plane. Calls run in the current
authenticated browser session and receive no capability that the corresponding
RCP surface lacks. There is no WebMCP tool for Proposal judgment, Decision choice,
graph editing or Sync, artifact retention or version undo, settings,
membership, or project creation/deletion. Provider answers, artifacts, and tool
output still cannot become canonical graph truth; only the ordinary typed Patch
and human-authority paths can do so.

## Voice agent

The voice button opens one GPT-Live session for the member's page; a second
click ends it. It works in the desktop app, the team browser app, and the team
phone web app. Audio goes between the page and OpenAI over WebRTC; RCP never
receives it. RCP keeps the session's text and action receipts as a
member-private record ([decision](../decisions/2026-10-08-voice-keeps-member-private-text-transcripts.md)).

`POST /api/voice/sessions` takes `{sdp_offer, tools, playbook, resume_id?}`,
where `tools` is `catalogAsFunctionTools()` with a size cap and `playbook` is
the page's RCP playbook, bounded by `VOICE_PLAYBOOK_MAX_CHARS`. The backend
reads the member's OpenAI preset connection that holds the `voice` purpose, and
creates a Live session with Responses delegation, `parallel_tool_calls: false`,
non-strict function tools (strict mode would make every optional field
required), the member's delegation model, and RCP's fixed instructions followed
by the playbook, for both the live and the delegated model. Only after OpenAI
answers does it create the session record, or for `resume_id` claim a new
generation of that record; an upstream failure changes no record. The claim
lands when OpenAI answers, before the page applies that answer, so a Resume
whose page then fails to connect has still taken the record and the older page
ends as superseded; Resume again recovers it. It returns
`{sdp_answer, session, input_truncated, limits}`. With no such connection it
returns `voice_not_connected` (409); an OpenAI failure returns
`voice_upstream_failed` (502) with a bounded message. `limits` carries
`idle_seconds`, `hard_cap_seconds`, `confirm_timeout_seconds`,
`commentary_max_chars`, and the transcript bounds from `limits.py`, and the
page enforces them.

The session record lives with the member's service connections on the current
space's backend and keeps the newest 20 sessions, each until 30 days after its
last update; member removal deletes it. `GET /api/voice/sessions` lists record
metadata with paging, `PUT /api/voice/sessions/{id}` saves entries and receipts
under the record's generation and a rising revision, `GET
/api/voice/sessions/{id}/generation` lets a page learn it was superseded, and
`DELETE` removes one. A stale revision, a superseded generation, a deleted
record, or another member's save is refused. Entries hold speaker (`member` or
`agent`), text, provider order, and the source a quoted read came from;
receipts hold the tool, its target, the accepted task or episode id, and the
outcome. Resume passes the record's text as `session.input` (user and assistant
messages only, at most 128 messages and a byte budget that guarantees OpenAI's
8,192-token limit, newest kept), keeps the unknown-outcome fence of every
unresolved receipt, reloads accepted task and episode ids into the watch loop,
and speaks one fixed summary of what finished and what still runs. The newest
Resume of a record wins; the older page ends with a notice. The voice panel
lists recent sessions with Resume and Delete, each tagged with the projects
its tools ran in (the page saves them with the record, at most 20). It is a floating window that opens
in the bottom-right corner; the member drags it anywhere and resizes it from
its corners, and the size is remembered.

Each voice write (a conversation Send, an Experiment Start, an Auto-research
authorization) carries a page-minted UUID4 request id. The page saves it in the
receipt before dispatch and sends it as the `Idempotency-Key` header. The
admission route records (project, member, key) with the admitted task or
episode in the same transaction as the admission; a repeat by the same member
returns the original result, and the key from another member or route is a
409. Rows are never pruned, because Resume renews a transcript's retention and
its receipts can resend a key at any later time; they go with their task or
episode.
`GET /api/projects/{id}/client-requests/{key}` returns the admitted ids (an
Experiment start also names its episode, so Resume watches the loop), or 404
when nothing was admitted or the row is not the caller's. Resume turns a found
key into an accepted receipt with a watch; a 404, which may be an admission
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

## Application surfaces

### Overview

Overview shows current project state and latest plain-language revision summary.
History names the canonical list **Project revisions**, attributes new records
from their stored snapshots, labels legacy records **Unattributed**, and derives
truthful operation fallbacks without inventing causality.

### Inbox

Inbox contains pending protected-belief Proposals, Decisions in `ready` or
`revisit`, and asserted open Blockers. A Proposal keeps inline judgment because
it is not a node. A Decision row opens the existing node-detail ballot. That
ballot marks the prior choice on the matching option, and shows it separately
when no option matches: an agent may reword a Decision's options but never
writes `selected_option`, so a reopened ballot would otherwise not say what is
being revisited. Accepted or contested open Blockers remain graph state but
leave human attention.
Historical Ambiguities never render or count.

### Research

Research presents question-centered paths and a bounded DAG. Research flow uses
one column per node type, ordered ResearchQuestion, Hypothesis, Decision, Blocker,
Experiment, then Evidence. All question depths share the ResearchQuestion column;
relations affect ordering within a column, never type placement. Columns start at
the same top row. Manual pins remain explicit position overrides.

The node detail is a persistent, resizable, viewport-clamped inspection window.
Its stable vertical one-hop relation map shows incoming neighbors, focus, and
outgoing neighbors without a nested scroll area. At most two comparison windows
remain open. Full-screen relation inspection does not navigate or add authoring
authority. Entering Agents closes node detail.

### Runs

Episode Run requests expose `code_worktree` and `graph_isolation`.
Auto-research `code_worktree` is optional and nullable. It resolves an omitted or
null code choice to true when eligible, otherwise false. It defaults graph
isolation to true and refuses `graph_isolation: false`. Experiment starts
default both to false; graph isolation creates an episode branch. An existing
branch target retains its branch and owner. Explicit ineligible code isolation
still refuses with its admission code.
Only the API start route resolves the omitted Auto-research code default.
Internal start and run requests default code isolation to false and do no
eligibility work unless enabled. Disabled admission does not read the manifest
or probe Git.
The episode response publishes both choices and `isolation_owner_episode_id`.
The binding is backend-owned; clients cannot supply worktree paths or an owner.
Resume, Retry, and Add N turns keep the captured choices and owner.
These fields have no Run-panel controls yet.

Runs is the episode ledger. Its primary object is the durable Experiment-loop or
Auto-research episode parent, never an invocation, graph node, or Blocker. It has
three sections in order: **Needs Action**, **In progress**, then **Completed**.
A section count must be truthful, so Needs Action holds only runs that have
stopped advancing and stay stopped until a human acts; a run still moving on its
own belongs to In progress however slowly it moves, and completed and stopped
history goes below. Auto-research placement reads the generic episode
projection. Experiment-loop placement, health, and next step read the existing
`ExperimentControlState` for the owning Experiment; generic episode lifecycle
fields never override that specialized backend answer.

Needs Action and In progress are each one unfolded reverse-chronological card
list containing both episode modes. Completed groups episodes by mode in
foldable lists, ordered **Experiment loop** then **Auto-research**. Seed/Refresh and ordinary task history
remain in project History; Blocker judgment remains in Inbox.

Experiment and Auto-research parents each expose one backend-decided health and
one separately labelled **Recommended next step**. Health, recommendation, and
`blocked_reason` come from one exhaustive table in the episode projection,
evaluated in precedence order: an episode with an ending is never `active`; a
`wrapping_up` status or a pending or running wrap-up reads `wrapping_up`; an
exhausted or human-pause ending reads `needs_action` with
`blocked_reason=reauthorize`; a failed control task whose failure kind is a
revoked login reads `needs_action` with `blocked_reason=sign_in` beside its
recovery control; an Auto-research recovery that stopped because its reattempt
failed the same way again reads `needs_action` with
`blocked_reason=repeated_failure` beside a recovery control that offers a
changed provider, model, or reasoning. The recovery summary carries the
`failure_kind` that stopped it, so the projection separates a revoked login from
every other stopped recovery without re-reading the diagnostic. `blocked_reason` names the one human action that clears a
block and is `null` otherwise; the card renders it as one lead sentence before
the recommendation. `continues_episode_id`, `continued_by_episode_id`, and
`can_continue` publish the continuation chain, and `chain` lists every member
oldest first with its ceiling, turns used, ending, and report summary, so the
card never rebuilds a chain from the bounded episode list; the card shows a
chain as one run with each member's ceiling, ending, and report in sequence,
offers **Add N turns** only where `can_continue`, and the timeline spans the
chain with a `continued` boundary. The branch summary names the chain root as `episode_id` and the
newest member as `current_episode_id`; `merge_requires_end` is gone. A wrap-up state of `not_started` on a settled episode means
the ending had no report to generate and reads like a skipped report. A
wrap-up whose report account is signed out reads `wrapping_up` with
`blocked_reason=sign_in`.

Provider login state is published at `GET /api/providers/logins` (every
machine account, with the project machines that use it, a secret-free summary
of a stored credential, provider-owned labels and supported sign-in methods,
and any running sign-in) and inside each
project's readiness snapshot as `provider_logins` (the accounts that project
uses). While any account is `signed_out`, the project Runs view and the space
landing render one `ProviderLoginNotice` per account naming the provider,
machine, time, bounded diagnostic, and a **Verify sign-in** control, and point
at Settings for the sign-in; the Experiment board's `reauthenticate_provider`
copy points at it. Space Settings carries a **Provider logins** card
(`ProviderLogins`) for both space kinds with one row per account: state, who
changed it and when, **Sign in with device code** when `device_code` is supported
(the code and link render while `GET .../sign-in/{login_id}` is polled), a token
field and **Sign in** when `token_entry` is supported (the token is sent once
and never read back; saving it is the sign-in, because the server verifies it
with one real request before the account counts as signed in), **Verify
sign-in**, and **Sign out**. **Verify sign-in** is withheld while a pasted token
is unsent, and from an account that has saved no credential and offers no
sign-in method but `token_entry`, because there it can only fail. Unknown
interactions do not inherit another provider's form. The backend supplies the provider label,
token instructions, and safe credential metadata; React never selects login
behavior from a provider name. Generic provider routes reject unsupported
actions and unknown request fields. Status GET reads status only: verification
completion resumes eligible parked work through the shared account lifecycle
and existing recovery owners even when the member leaves Settings or closes
the page. Durable reconciliation repairs interruption before resumption without
duplicate launches. Every action is available to any signed-in member
and records that member. Task
status, phase, workers, and diagnostics remain supporting history rather than
competing primary states.
For a terminal Experiment episode, the owning node's human-authored closed status
is authoritative: the run is Completed and fresh-start control is absent until
the node is edited back to a nonterminal status. A control is absent unless
currently valid, and no recommendation names an unavailable action. A
recommendation follows the named cause of the latest failure, not only its
shape: a revoked provider login asks the human to sign in again rather than
offering a Retry that cannot succeed. A live episode whose latest turn failed
states that failure on the card face, where an ended episode states its ending
diagnostic; neither leaves the reason folded away in task details while the card
recommends acting on it. The card reads the backend's recommendation, never the
failure kind itself, which the response seals. Report
availability is separately backend-decided from the newest report-bearing
episode for that Experiment and exact graph target; a newer no-report episode
does not hide the durable report or change which episode owns it. The backend
publishes `report_is_current` alongside its owning episode id. Runs renders
**Previous episode report** when false and **Open report** when true, so an older
retrospective is not presented as the current episode's report.

In a personal space on macOS, Space Settings also carries a **This Mac** card
(`ThisMac` in `SpaceSettings.tsx`) from `GET /api/machine-power`: one toggle
for the idle hold and a status line with the demand reasons. `PUT` changes
`idle_hold`.
The card also has a **Lid-closed mode** toggle that opens the opt-in dialog and
install when not installed, **Uninstall**, and extends the status line with the
mode, last release, latch, external owner, and install problem. The closed lid
still sleeps the Mac unless lid mode is active. `PUT` also changes `lid_mode`;
enabling it clears a latch. Install and uninstall are `POST`s; a cancelled
admin prompt returns 200 with the unchanged status. The space landing shows a
warning only while lid mode is latched off or a cleanup failed, with the exact
command and a copy button.

Episode cards lead with the owning Experiment name or Auto-research identity;
their start time is secondary metadata and is never prefixed with a redundant
`Episode` label. A completed type group names the mode once rather than repeating
it on every card. Collapsed cards contain no muted recommendation or report
commentary. Each Experiment's backend control selects its one current
`episode_id` per graph target, so repeated work on one target produces one card,
and live loops on different targets produce one card each in the same flat
list. Selection, Stop, and busy state follow the exact episode. Older episodes
remain reachable through project History instead of appearing as sibling Runs
cards.

Each Experiment card shows its graph target (a Main pill or a branch badge), its
checkout (shared or worktree), and who started it: a member, or Auto-research
with a link to the parent run. Each episode card and space run row also shows a
compact initials avatar and the recorded human authorizer's name. This is
historical episode attribution, including the inherited authorizer on an
Auto-research child; it does not claim live presence or enumerate contributors.
Missing legacy attribution never borrows the current viewer's identity. The
Run dialog lists live loops on that node on other targets before submission,
as information; it never disables Run. A main loop opened from a branch view
shows its chat read-only. A chat's watcher strip lists only live watchers (watching, check
failing, or a job that can still be cancelled) that chat armed or its own
target's loop owns; ended watchers stay in Runs.

Every unarchived episode offers **Archive**; an archived episode offers
**Unarchive**. The [episode archive](conversations-episodes-and-watchers.md#episode-archive)
is shared across the project. Archived episodes are absent from default Runs
cards, section counts, and nested child-Experiment links. **Show archived** adds
a separate **Archived** section without changing the active section counts.
An archived older Experiment episode remains available there with its own
identity, History entrance, and Unarchive control even after a newer episode
owns the Experiment's operational controls. Archives remain discoverable beyond
the recent-episode list limit and the space ledger's seven-day completed window.
Archive metadata on cached controls is refreshed from current storage before
being published; cached graph state cannot reverse an archive choice.

The episode index is an explicit typed projection whose current `episode` is
non-null. Main-target entries consume the completed project snapshot's
Experiment-control map; branch entries consume the exact branch read model.
Both `GET /api/episodes?mode=experiment_loop` and the project-scoped index
return `{entries, unavailable}`. Healthy entries remain visible when a live
loop's branch or control cannot be read. Each unreadable live loop has an
`unavailable` row with project identity, graph target, control node id, episode
id, and diagnostic detail. Historical-only failures are logged and skipped.
Runs shows a small notice naming the affected branches. A missing or invalid
required cached project snapshot still returns 503. `/api/space/runs` keeps its
existing response shape and returns 503 with the first unavailable detail.
Episode task rows publish durable actor `role` and lineage `depth`, and episode
cards consume those fields without interpreting persisted task requests.
The page keeps each project's episode list, so returning to a project tab shows
that list at once while it refreshes. A poll never overlaps a list request
already in flight for the same project; it waits for that request instead.
Project Runs refreshes this index while visible, so an Experiment dispatched on
an episode graph branch appears as its own episode card even before anyone
opens its exact route. The project-scoped
`/api/projects/{project_id}/experiment-episodes` path restricts projection work
to that visible project. The same child appears once as a linked, subordinate
child actor on the owning Auto-research card's timeline. That actor is
navigational provenance, not a second lifecycle or budget: its label and status
consume the indexed node and control, while the child card retains its own
episode budget, transcript, and valid controls.

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

The roster replaces the old Turns and Mail lists and keeps the message composer
beneath it. It links turn and attempt spans to the existing task inspector,
excludes reports from that navigation, and resolves child Experiment navigation
by episode id. Watcher controls remain with their existing owners. Layout,
selection, and gesture behavior follow the
[interface specification](interface-and-visual-design.md#auto-research-and-episode-history).

An active child card names its current Experiment turn and links that row to the
ordinary task inspector. Until the turn finishes, the card labels the durable
objective separately from retained stale guidance. When the backend finds the
matching active Auto-research graph condition, the Experiment index publishes
`parent_watching`; the card explains that the owning episode watches the child
while the child's own **Watchers** fold contains only detached work handed off
by that Experiment.

Starting an Experiment navigates to its Runs detail rather than opening floating
chat. The detail separates historical episode budgets from **Next episode
limit**, shows watcher/session/host continuity, and omits semantic attempt
history from the node drawer. Stop, recovery, wrap-up, and report presentation
follow the episode specification.

An explicit main-target Experiment route binds only when both its `episode_id`
and graph target still match the loaded backend control. If the control advanced,
the route shows a History handoff without exposing the newer episode's transcript
or controls.

An Auto-research detail shows compact graph-branch identity, base/head, merge
state, and a persistent **Merge** panel. Experiment cards that own a graph
branch or code worktree show the same panel. A deliberate click checks
current server eligibility and either starts the merge or displays the blocker
beside the control, following the
[branch merge projection](auto-research-and-branch-merge.md#runs-projection).
The Experiment Run control offers "Work on a graph branch" and "Code worktree"
toggles, off by default. The Auto-research dialog shows the graph branch as
always on; its Code worktree choice is left to the server's eligibility check
unless the human opts out. On a branch, each DAG node shows its title and one
word: how Merge will treat it (Conflict, Needs agent, Proposal, Already merged),
else how the branch changed it (Added, Changed, Removed). Standing, status, and
the connect handle appear on hover. Node detail lists each changed field as
main before, branch, and main now beside a conflict, each with its own color.
**Open graph** selects the branch workspace explicitly. An exact branch
Experiment route may show its historical transcript through Runs without exposing an ordinary
composer for that episode-owned session. Ordinary chats started in the branch
workspace have their own sessions and authority. A main workspace cannot reuse
a branch-bound conversation or native session.

The space project index reuses these same backend lifecycle projections in a
summary ledger. It does not derive a second status machine from task or episode
fields. Its seven-day completed window is presentation-only; active and
actionable parents remain visible regardless of age, and project-scoped Runs and
History retain their existing complete records.

### Agents

Agents (route view `chats`) groups project and node conversations. Every human and assistant turn
keeps its immutable Discuss/Work label; progress stays inline under the triggering
message. There is no global task banner. The composer and history remain usable
while unrelated background tasks run. A running watcher wake or episode turn has
no human message, so it holds an Activity row in its chat until its first output.

Agents lists every conversation in the project: chats on main and on every graph
branch, Experiment episode conversations, Auto-research children, and
conversations whose first turn has no transcript yet.
`GET /api/projects/{project_id}/chats?inventory=true` returns that whole inventory
as one snapshot from the retained main service's cached scan, so a conversation
updated between requests is never skipped; each summary carries its graph
target, a graph title, and a `conversation_kind` of `chat`, `episode`, or
`auto_research_child`. Rows show those tags. A filter beside the search picks
all, main, or one branch, and applies to the whole inventory. The Archived
column starts folded. Read markers and unread finishes cover the whole project.

Selection stays exact-target. Opening a row on another graph switches the
workspace through `#/projects/P?view=chats&chat=C&branch_id=B` before the
composer mounts; node actions select only within the viewed graph. A
conversation a task names before the inventory lists it has unknown ownership:
its composer stays closed and the inventory is fetched again. Posting
follows the ordinary chat rules on that target. An Auto-research child's chat
is read-only: it shows its transcript and a link to the orchestrator in Runs,
and task admission refuses a human turn on it with
`auto_research_child_read_only`. The composer explains
`episode_merge_reserved` and `episode_isolation_unavailable` refusals.

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
also returns `latest_finished`, each unarchived chat's newest finished turn, so a
reply older than the task list window is still found. A chat is unread when its
newest finished turn ended after its marker. Viewing a chat
posts its newest finish time to `POST .../chats/{chat_id}/read`; the marker only
moves forward. Markers are not moved with a transferred project.

### Paper, Settings, and History

Paper owns human Markdown Write/Preview and read-only coaching. Settings has two
levels. Space Settings, opened from the gear beside the identity menu, owns the
server status (team spaces), machine cards with their names (renamed in place) and
writable paths, provider logins, and the
personal space's clear-all-caches. Project Settings owns repositories, this
project's machine cards (provider paths, compute, and the same writable-path
record), execution profiles, compute connections, packages, caches, project
membership, and prospective episode limits, not ontology authoring. Display
preferences live in the identity menu.

Team repository descriptors include `source` (`github` or `server_only`),
nullable `github_identity`, and `can_connect`, from `effective_repositories`;
only a server-only team repository has `can_connect` set. The display
completion path reads this operational provenance afresh even for cached graph
snapshots, so a completed Connect is immediately reflected. Personal
repositories, and team repositories whose provisioning evidence cannot be
resolved, omit all three fields: nothing claims a server-only state or offers
Connect without proof. These fields describe RCP-managed provisioning, not
discovery of a personal checkout's Git remotes.

`/api/space/machines` lists, creates, renames, and deletes machine cards; a card
in use by any project, or whose use cannot be established, cannot be deleted,
and host and account never change. A `PATCH` of `writable_paths` validates each
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
execution-machine/connection matrix with distinct unreachable, authentication,
and host-key states. Normal readiness reads reuse the last result; only an
explicit refresh runs network probes, and a connection or execution-binding
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
becoming an empty or healthy panel.

That route is GET-only and available only to an authenticated team member.
`?refresh=true`, sent by the panel's refresh button, only shortens the release
lookup bound to 1 minute. It
does not configure or run backup, update, restore, Git credential preparation,
project provisioning, provider login/check, or member removal. It also does not
enumerate the console operations that do: an operator holds machine authority
already and reads `rcp server --help` on the machine, so restating that
catalogue in a member-visible panel adds a second place for it to drift. A
command still appears in the Web surface where one specific request needs it,
carrying that request's own identifier. D6's separately proved desktop operator
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

Graph-writing agents add or revise glossary terms through `upsert_glossary`
Patches. Canonical definitions render inline; there is no standalone Glossary
view or human glossary editor.

## Desktop shell

The desktop window is a client of backend-owned durable work. Closing or hiding
the window never cancels a task. Reopening attaches to the healthy owner and
current app state.

A source-mode frontend rebuild preserves content-hashed assets from the prior
build so an already-open window can keep lazy-loading its coherent bundle until
the researcher reopens it. Packaging and server startup use a clean build. If a
chunk is nevertheless unavailable, the client performs one bounded document
reload; a repeated failure renders an explicit reload action instead of a blank
window or reload loop.

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
instruction; a custom server lists every id, named ones first. A failed listing returns `model_list_failed` (502) and the card falls
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

The client records with the first accepted format `MediaRecorder` supports,
pins the connection id when recording starts, and replaces the dictation span
with the returned text in one step; typing drops a late result. Native
dictation reports `preparing` while macOS downloads the on-device model and an
`engine` (`speech_analyzer` or `apple_server`) on `recording`; every result
carries the whole session text. `desktop_stop_dictation` takes `finish`: Stop
delivers the final result before `stopped`, invalidation cancels. SpeechAnalyzer
needs only microphone access; speech recognition permission is requested only
before the older recognizer. A desktop build without the macOS 26 SDK skips
SpeechAnalyzer with a warning; release builds require it, and CI fails if any
macOS 26 Speech symbol or Swift library is a strong import.

### Update notice

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
prebuilt app shows a Download button only once the companion is confirmed.
A published prebuilt app has the Tauri updater enabled and checks it at launch
and whenever its window is shown; while that check reports a newer release, the
surface shows the native **Update** button instead, which replaces and
relaunches the app and asks before interrupting running agent tasks.
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

## Provider sign-in resume response

Verified sign-in, token save, and successful device sign-in retain
`resumed`, now `{ "checked": N }`. This is a coarse count of episodes inspected
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
the app's default appearance for an unknown value. Every inline frame speaks
protocol version 1 over `postMessage`:

- content to shell, and shell to chat: `rcp-artifact-size` with a numeric
  `height`. The wrapper relays only this number from the opaque page; the shell
  and the chat each clamp it to the inline bound.
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
child Experiment episodes. It includes unexpired or kept artifacts and permanent
reports, with the queried episode's report first, then creation order. Worker
labels use the child Work route's instruction heading, or Worker. Both routes enforce project
membership, and the inventory rejects an episode from another project.
