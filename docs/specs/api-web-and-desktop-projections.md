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
receive tokens. Responses carry `epoch`, one `as_of`, per-resource `key`,
`version`, `valid_until`, and `state`, plus remote sync health.

Set-based SQLite reads cover authorization/aliases, transactional counters,
indexed deadlines, and, when progress is watched, which projects have a running
task. The route never opens a project, replays history, completes a snapshot, or
waits for snapshot locks or SSH. File families use published catalog
generations; terminal runtime uses its owner's generation. Graph tokens fold in
whether sync health reports an error, because the graph payload carries
canonical reachability. While a task runs, `tasks.progress` and task details
take a clock tick, because elapsed time and progress are computed from the clock.
Entity counters are project-scoped; chat read/pin counters are member-scoped.

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
episode-owned branch, newest chain-member creation first. It reads all episodes,
including archives, without the recent-50 limit, under project membership.

Rows carry `kind`, `branch_id`, `head`, `base_head`, `episode_id` (chain root),
`current_episode_id` (newest member), graph-isolation `archived`, and merge fields
`merge_state`, `merge_eligible`, `merge_blocked_reason`,
`latest_successful_merge`, `active_merge_task_id`, and `merge_diagnostic`.
Branches also carry the root's `mode`, `created_at`, resolved `name`, `running`,
and `needs_you`; there is no `title`. Running means an active task on that target;
attention means an open Decision/Blocker or answerable question. Names use the
shared SQLite owner with date fallbacks. `branch_id == episode_id` names the root.
Main has its exact head, name `main`, false flags, and null branch/episode/base/
merge details. Merge fields use the episode owner's reservation and writer checks.

`PUT .../graph-refs/{branch_id}/name` takes `{"name": str}`;
`PUT .../graph-refs/{branch_id}/archived` takes `{"archived": bool}`.
Both admit project members and return the GraphRef. Rename returns 422
`branch_name_invalid` or 409 `branch_name_taken`; unknown branches return 404.
Main cannot be renamed or archived. Archive creates missing isolation state,
without changing Patch history or episode-history archive state.

The `⑂ <name>` picker sits after view tabs, or beside the project name on phones.
It hides on main with no live branch. An amber dot signals attention on another
unarchived branch. Rows order main, running, then newest; hide archived branches
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
chat attachments are the exception. JSON and transfer-proof octet streams remain
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

Project terminal routes under `/api/projects/{project_id}/terminals` are:

| Route | Contract |
| --- | --- |
| `GET /repositories` | Repository/start path, machine capability, running Work identities. |
| `GET` | Open sessions and live/idle state. |
| `POST /probe` | Invalidate machine probes and schedule fresh results. |
| `POST` | Open or return the repository's session; `require_new: true` returns 409 if one exists, under the manager lock. |
| `DELETE /{session_id}` | End the session. |
| `WS /{session_id}/ws` | Binary PTY output; JSON `input` (`data`), `resize` (`cols`, `rows`), and `ended`. |

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
episode/consolidation artifacts. Jobs, failed tasks, ended episodes, lessons,
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

Attention publishes exact Proposal, Decision, and asserted-open-Blocker ids.
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

Node and project chats show resolved cards read-only in transcript order and
open question cards at the end of the transcript, scrolling with it. The
composer retains its steering behavior.
A single choice submits immediately; multiple choices use toggles and explicit
submission. Free text remains available with either choice format. A parked
chat answer with no running turn names Discuss or Work continuation on its submit
control, matching the asking turn's capability.
Experiment and Auto-research detail show the same cards; withdrawn cards retain
their history with an **Episode ended** state. Chat and episode views select
questions from the shared project resource, refreshed by its watermark and
invalidated by question commands. Fresh pending rounds show a cobalt **Agent is waiting**
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
reads remain available, and Patch history is unchanged. The default episode
list is the 50 most recent plus retained archives and the current member of
every Auto-research chain, so each orchestrator card in Agents has its episode.

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
completion does not trigger a reload bundle. Paper is an ordinary profile with
its own [draft-version guard](paper-artifacts-and-result-views.md#human-paper-authorship).

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
are never canonical input.

## Project index and identity

The index leads with project cards, then a space Runs ledger across visible
projects. Backend projections select Experiment parents and Auto-research chains,
health/section, project identity, and exact routes. Needs Action/In progress mix
modes newest first; Completed folds by mode and keeps seven days without deleting
records. Exact Auto-research parents outside bounded lists are fetched separately.

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

Before enrollment, token exchange, project-card reads, or cookie installation,
the shell negotiates the highest overlap of inclusive integer protocol ranges
and requires the exact echoed integer. Missing/mismatched negotiation or changed
`space_id` refuses the connection; diagnostics name both source commits and a
compatible release/update path. Semantic versions and commit order are not the
protocol. Personal work remains usable; unavailable team cards remain labelled.

Desktop supports `[3, 4]`, server `[1, 4]`. Protocol 1 forbids team deletion;
2 permits deletion only with negotiated support and the exact confirmation;
3 adds optional reviewed `source_commit` and `rcp-transfer-v2` Git archives,
while accepting omitted commits/v1 archives; 4 adds optional
`record_schema_version=2` for episode archives. Archive-free transfers retain
prior bytes; incompatible peers refuse preparation before source release.
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
match; failed/changed tunnels back off before replacement.

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

Backend contact failure keeps the rendered page beneath a blocking overlay,
preserving panels, scroll, and text. Same version/instance/data-directory/space
recovery reconciles normally; changed identity requires explicit reconnect and
a fresh project open. Ordinary browsers can use a reachable team UI but cannot
own tunnels, credentials, multi-space routing, or native commands, and do not
offer Add team space.

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
(alias, optional source, machine alias, truth choice) or `connect_repository`
(alias, source). Existing project membership gates reads and requester confirmation.
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
Inbox within the current project/target. First-turn chats open from their task.
Run ids resolve through notification routing. Only `rcp_open_project` changes
project; opening a view never changes branch.

Calls use the member's authenticated page and existing owners. There are no tools
for Proposal judgment, Decision choice, graph edits/Sync, Keep/Undo, settings,
membership, or project creation/deletion. Voice gets no browser/page-driving tool:
it could press its own Confirm and send the screen to OpenAI.

## Voice agent

The voice button opens one GPT-Live session for the member's page; a second
click ends it. It works in the desktop app, the team browser app, and the team
phone web app. Audio goes between the page and OpenAI over WebRTC; RCP never
receives it. RCP keeps the session's text and action receipts as a
member-private record. It survives sign-out and provider disconnection, but
member removal deletes it. Privacy is enforced between members through RCP;
the service account can read the files. Records stay outside application backups.

**Why:** Text seeding resumes a dropped conversation without OpenAI-side storage;
receipts prevent repeating work after a lost response. Resume creates a new
session with `store: false`, preserving text but losing audio tone/timing.
Seeded history is context, never write authority or proof of current status;
`store: false` is not a zero-retention promise.

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
`blocked_reason` identifies the clearing action or is null. The card shows its
cause and diagnostic before the separately labelled recommendation; controls
appear only when valid. An ended Experiment whose human-authored node is closed
is Completed with no fresh Start until reopened. Report selection uses the newest
report-bearing episode on the exact target: `report_is_current` distinguishes
Open report from Previous episode report. A newer reportless episode cannot hide it.

Continuation fields and ordered `chain` publish every member's ceiling, usage,
ending, and report. One card represents the chain; Add N turns follows
`can_continue`, and timeline boundaries mark continuations. The branch root owns
`episode_id`; its newest member is `current_episode_id`. Settled `not_started`
wrap-up means no report was needed; sign-in-blocked reports remain wrapping up.

`GET /api/providers/logins` lists machine accounts, dependent project machines,
secret-free credential summaries, labels, supported sign-in methods, and active
sign-ins. Project readiness includes its relevant `provider_logins`. Signed-out
accounts get one notice on Runs/landing with provider, machine, time, bounded
reason, Verify sign-in, and a Settings link. Space Settings offers only the
backend's methods: device-code flow with polling, one-shot token entry verified
by a real request, Verify, and Sign out. Verify is hidden while a token is unsent
or a token-only account has none saved. Provider names never select UI behavior.
Generic routes reject unsupported actions/fields; GET only reads. Successful
verification reconciles parked work even after leaving the page, with durable
repair and no duplicate launches. Any signed-in member may act and is attributed.

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
older episodes stay in History. Cards identify target, checkout, starter, and
historical human authorizer; missing attribution never borrows the viewer.
Selection, Stop, and busy state bind the exact episode. Other live target loops
are informational in Run, not a launch veto. Main-loop chat opened from a branch
is read-only. Chat watcher strips show only their own/live target rows; ended
watchers remain in Runs.

[Episode archive](conversations-episodes-and-watchers.md#episode-archive) is shared.
Archived entries leave default cards/counts/child links; Show archived adds a
separate section, retaining each episode's identity, History, and Unarchive even
outside recent/space windows. Storage refreshes archive metadata on cached controls.

Both `/api/episodes?mode=experiment_loop` and project
`/api/projects/{id}/experiment-episodes` return `{entries, unavailable}` with
non-null current episodes. Main entries use completed controls; branches use
exact branch read models. Unreadable live loops yield identity/target/node/episode/
diagnostic rows without hiding healthy loops; historical failures log and skip.
Required invalid project caches return 503; `/api/space/runs` returns 503 on the
first unavailable detail. Task rows publish role/depth rather than client parsing.
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

Isolation-owning cards show branch base/head, merge state, and persistent Merge.
A click checks current eligibility; [merge projection](auto-research-and-branch-merge.md#runs-projection)
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
column starts folded. Read markers and unread finishes cover the whole project.

Selection stays exact-target. A link to a conversation on another graph switches the
workspace through `#/projects/P?view=chats&chat=C&branch_id=B` before the
composer mounts. An orchestrator selected from main's Agents opens in place
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
branch, identified by the chain root's episode id (the branch id) and named by
the branch. Its transcript is projected from the chain's orchestrator-role
turns: each turn's starting instruction or the human mail it delivered, then its
answers; worker turns keep their own cards. Loaded turns supply live status and
progress, and the episode's `needs_action` health (an open question) files the
card under needs-you. The card has no chat composer: its composer, with the
episode's open questions, sends **Message orchestrator** to the newest chain
member, gated by that member's `can_message`, `message_refusal`, and
`message_requires_continuation` exactly as Runs is. The composer explains
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
state reports `pin_branch_id`, `can_pin`, and `pinned` for the acting member.
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
