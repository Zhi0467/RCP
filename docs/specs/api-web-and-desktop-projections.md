# API, Web, and desktop projections

This specification owns public project projections, current Web surfaces,
revision reconciliation, navigation and tab state, and desktop-shell lifecycle.
It does not grant graph authority; mutation routes delegate to the state
workspace and transition manager.

## API composition and mutation boundary

One FastAPI backend serves the JSON API and, when built, the React/Vite
application. The optional Tauri shell starts or reuses that same backend. There
is no second team protocol or frontend-owned background-worker runtime.

Route handlers resolve identity and membership, validate request shape, stage
intent, and call the owning service. They never write `.research` files,
materialized output, branch metadata, or Patch history directly.

Every graph mutation response uses one strict transition projection containing
the graph target, head, graph, Experiment-control map, guidance validity,
transition/ruleset identity, and any causal or attention inputs from the same
final state. Preview responses are explicitly noncanonical and name their base
head and ruleset.

Project snapshots and transition projections publish exact graph-attention
membership as pending Proposal ids, Decisions awaiting choice, and asserted
open Blocker ids. Counts are lengths of that same projection. The browser maps
those ids onto the graph it is presenting; Inbox, Overview, and Runs never
reapply the membership predicates. A backend preview supplies both the candidate
graph and candidate membership, while a rule-inert local draft retains the
current backend membership until Sync. Cached snapshots are invalid when their
membership or the three corresponding counts disagree with their graph. The
browser validates the exact projection shape and referenced graph member types;
missing or malformed membership fails the snapshot instead of becoming an empty
attention view.

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

Concurrent project snapshot requests are fenced per project by start order,
including equal-revision responses. Once a newer cache, reload, watcher poll,
settings save, or Sync starts, an older response cannot overwrite its graph or
operational controls.

Every branch route proves the branch belongs to the requested project and
episode. A branch id alone never grants lookup. Task, watcher, episode, and
Experiment detail APIs preserve exact `main` versus `branch:<id>` target
identity.

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

Resolved/superseded Blockers remain canonical but are omitted from active
Research-flow and attention projections. Stale Experiment summaries and next
actions are labelled historical and never rendered as current guidance.

## Revision observation and drafts

Visible clients notice canonical main changes without browser reload or
repurposing the Seed/Refresh action. Every open project tab sends a cache-only
heartbeat on the bounded visible cadence; the active tab observes completed
cached revision updates more frequently, and visibility resume sweeps all tabs.

A heartbeat may schedule one bounded lock-free, single-flight remote-head probe
per project. An unchanged or temporarily unavailable head does not replay or
copy the graph. Movement starts background reconciliation; an older result may
not replace a newer cache.

Reconciliation preserves human drafts. A staged node whose canonical revision
did not move stays committable. One that moved becomes behind and is excluded
from Sync until the human edits it or reversibly swaps an incoming field into
the draft. Paper drafts use their own equivalent whole-document rule. Display
caches never become canonical input or graph authority.

## Project index and identity

The project index keeps project cards first and one distinct cross-project
**Experiments** board below. The board includes only Experiments with loop
history, orders actionable/active work before folded finished work, preserves
last-known rows for unavailable projects, and navigates to the owning project's
exact Runs detail. It grants no control outside that project.

The index header contains the compact current-human identity control. An unnamed
personal owner sees **Sign in** as naming the durable local identity, not creating
an account. The panel shows editable display name and exact read-only copyable
user id. A team member uses the server login/session boundary and can manage
their own credential. Pending project invitations appear on the index.

Projects hidden by membership never appear as locked cards. Losing access
closes its open tab and returns to the index.

## Confirmed team desktop target

The pending first team client is the source-built desktop app. Its local project
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
health, the expected `space_id`, and `minimum_shell_version`. It exchanges the
stored member token and establishes the team server's HTTP-only session in the
WebView before navigating to that server's own application. A changed
`space_id` blocks mutations until the human explicitly reconnects; an
unavailable team connection leaves personal work usable and shows its cached
cards as unavailable. Returning to the local index reloads the local backend.

Every saved space receives a stable, distinct loopback origin. Different ports
on the same `127.0.0.1` host are not isolation because cookies ignore ports; such
tunnels would collide on the shared `__Host-` session-cookie name. The shell may
use only an origin mechanism proven against the real WKWebView; navigation
admits only origins derived from its saved connection registry, and a live drive
must prove that cookies stay separated between two simultaneous team spaces.
The HTTP loopback-alias and exact-host control failed the `Secure`-cookie gate,
while the extra-address path could not reach WKWebView on stock macOS. The
mechanism therefore remains open in
[Q11](../open-questions.md#q11--how-should-the-desktop-provide-isolated-secure-local-origins)
without weakening this security boundary.

The ordinary browser can use the team server UI when transport already exists,
but it cannot own multi-space routing, credential storage, SSH tunnels, or
server-command execution. Source mode is the supported client for this slice; a
packaged Linux client is not required.

Project creation and transfer use one visible project wizard. Its three named
intents are **Use an existing checkout personally**, **Create a shared team
project**, and **Move an existing personal project to a team**. An entry point
may preselect an intent, and Project Settings deep-links to the move intent, but
the user does not encounter separate personal, provisioning, and transfer
wizards. Each backend exports only its own product eligibility, preselection,
required fields, and any pinned source identity. The desktop-native bridge
separately exports relay capability and its authenticated saved team targets.
The wizard offers move only by intersecting explicit permission from the
personal backend, explicit admission from the selected team backend, and that
native capability answer. A browser has no native capability answer and cannot
offer cross-space move. The Web does not infer product authority from
`space_kind`, paths, saved-connection presence alone, or native-global
detection.

The modes retain their separate authority owners behind that shared surface.
Personal setup calls the ordinary path-based preflight/finalizer. Team creation
creates a backend-owned durable provisioning request from GitHub repository
sources and derived central paths. Personal-to-team transfer creates linked
requests in the two authenticated backends and is available only in the
source-built desktop because its native shell owns the archive relay. A direct
team request to `/api/projects`, `/api/project-setup/preflight`, or
`/api/project-setup/create` is refused before request-body interpretation,
filesystem inspection, or catalog mutation. The separately validated
provisioning finalizer is the only team-project entrance into the existing
setup/registration owners.

The backend health projection carries one `project_creation` control with all
three intent identities, per-intent eligibility and preselection, primary action
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
code. A local-only codebase is not uploaded through the wizard: the new-team
intent tells the human to push it to a GitHub repository with a real commit
through their ordinary Git workflow, then records that repository source. RCP
does not collect GitHub user authentication.

A saved member connection and an operator route are distinct capabilities even
when they use the same SSH host. **Run setup now** appears only in the desktop
after a native read-only probe proves it can invoke the fixed `rcp server project
provision <request-id>` command directly as `rcp` or through noninteractive
`sudo -u rcp -H`. The shell passes a validated request id as an argument and
never executes arbitrary command text returned by a server. If SSH or `sudo`
needs interaction, the app shows or opens the same command in Terminal; it never
collects a private key or privilege password. The browser shows a copyable
operator command instead.

CLI structured progress is presentation input only. The CLI reports each state
change to the lock-owning backend through its private local control channel, and
the Web UI refreshes the durable request. Only the final explicit human review
may create or re-home the project. For personal-to-team transfer, one desktop
review action calls both already-authenticated backends in a fixed order: target
admission first, then source release. Each backend records its own human actor;
the native relay and remote CLI cannot provide either confirmation. A partial
first confirmation is durable state that the same request resumes, not evidence
that the project moved.

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
is open.

An explicit Runs route is authoritative over cached selection, including a route
with an absent or malformed branch identifier. Invalid branch identity resolves
to no selected Experiment rather than restoring a cached main-target selection.

## Application surfaces

### Overview

Overview shows current project state and latest plain-language revision summary.
History names the canonical list **Project revisions**, attributes new records
from their stored snapshots, labels legacy records **Unattributed**, and derives
truthful operation fallbacks without inventing causality.

### Inbox

Inbox contains pending protected-belief Proposals, Decisions in `ready` or
`revisit`, and asserted open Blockers. A Proposal keeps inline judgment because
it is not a node. A Decision row opens the existing node-detail ballot. Accepted
or contested open Blockers remain graph state but leave human attention.
Historical Ambiguities never render or count.

### Research

Research presents question-centered paths and a bounded DAG. `has_subquestion`
depth forms successive ResearchQuestion columns, followed by
Hypothesis/Decision, Experiment/Blocker, and Evidence stages. Other relations
affect ordering but not question depth.

The node detail is a persistent, resizable, viewport-clamped inspection window.
Its stable vertical one-hop relation map shows incoming neighbors, focus, and
outgoing neighbors without a nested scroll area. At most two comparison windows
remain open. Full-screen relation inspection does not navigate or add authoring
authority. Entering Chats closes node detail.

### Runs

Runs is the episode ledger. Its primary object is the durable Experiment-loop or
Auto-research episode parent, never an invocation, graph node, or Blocker. It has
two sections: **Needs Action** first, then **Completed**. Active, recovering,
wrapping-up, failed, and otherwise actionable episodes stay prominent in Needs
Action; completed and stopped history goes below. Auto-research placement reads
the generic episode projection. Experiment-loop placement, health, and next step
read the existing `ExperimentControlState` for the owning Experiment; generic
episode lifecycle fields never override that specialized backend answer.

Needs Action is one unfolded reverse-chronological card list containing both
episode modes. Completed groups episodes by mode in foldable lists, ordered
**Experiment loop** then **Auto-research**. Seed/Refresh and ordinary task history
remain in project History; Blocker judgment remains in Inbox.

Experiment and Auto-research parents each expose one backend-decided health and
one separately labelled **Recommended next step**. Task status, phase, workers,
and diagnostics remain supporting history rather than competing primary states.
For a terminal Experiment episode, the owning node's human-authored closed status
is authoritative: the run is Completed and fresh-start control is absent until
the node is edited back to a nonterminal status. A control is absent unless
currently valid, and no recommendation names an unavailable action. Report
availability is separately backend-decided from the newest report-bearing
episode for that Experiment and exact graph target; a newer no-report episode
does not hide the durable report or change which episode owns it.

Episode cards lead with the owning Experiment name or Auto-research identity;
their start time is secondary metadata and is never prefixed with a redundant
`Episode` label. A completed type group names the mode once rather than repeating
it on every card. Collapsed cards contain no muted recommendation or report
commentary. Each Experiment's backend control selects its one current
`episode_id`, so repeated work produces one card for that Experiment node. Older
episodes remain reachable through project History instead of appearing as
sibling Runs cards.

Starting an Experiment navigates to its Runs detail rather than opening floating
chat. The detail separates historical episode budgets from **Next episode
limit**, shows watcher/session/host continuity, and omits semantic attempt
history from the node drawer. Stop, recovery, wrap-up, and report presentation
follow the episode specification.

An Auto-research detail shows compact graph-branch identity, base/head, merge
state, and **Merge to main** only for an eligible changed head. Main graph views
never switch to branch truth. An exact branch Experiment route may show branch
history and its transcript, but its chat/composer and repair controls are
read-only until a deliberate branch conversation authority is designed. Generic
main NodeChat cannot reuse the branch-bound conversation or native session.

### Chats

Chats groups project and node conversations. Every human and assistant turn
keeps its immutable Discuss/Work label; progress stays inline under the triggering
message. There is no global task banner. The composer and history remain usable
while unrelated background tasks run.

### Paper, Settings, and History

Paper owns human Markdown Write/Preview and read-only coaching. Settings owns
repositories, execution profiles, packages, caches, project membership, and
prospective episode limits, not ontology authoring. History and task detail own
complete provider attempts, stages, events, diagnostics, package versions,
answers, graph outcomes, and recovery chains.

## Causal and relation presentation

Causal layout is a read-only derived projection from the same graph revision.
Feedback strongly connected components rank together; layout never generates a
graph action or breaks a cycle by changing truth.

Evidence node detail presents observation, interpretation, role, validity,
origin, provenance, and artifacts. The Hypothesis relation/detail surface owns
each claim-relative direction, relevance, weight, scope, and qualifications.
Historical global strength is clearly legacy and never shown as current edge
weight.

Glossary terms already in canonical history render as best-effort inline
definitions. There is no standalone Glossary or current authoring path.

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

Preview links open the shell's secondary bounded window rather than navigating
the main project WebView. Desktop repository links and result/report artifacts
therefore cannot strand the main project window. Native downloads resolve
through shell-controlled destinations.

In project setup, every local repository path has a native folder action in the
desktop shell. Selecting a folder fills its absolute path; cancelling preserves
the current value. SSH paths remain manual, and the browser states that native
folder selection is available only in the desktop app.

The desktop may add shell-only dictation, update, reconnection, and packaging
behavior only where an active acceptance contract owns it. Browser verification
does not stand in for native window lifecycle.

A local Codex thread created through RCP's app-server runtime is stored by Codex
and may therefore appear in the Codex Desktop task list. RCP uses that as an
inspection surface only. Sidebar ordering, loading, takeover, and concurrency
remain Codex Desktop behavior rather than RCP product state.

## Frontend trust boundary

Provider naming is a backend answer. Readiness exports each provider's runtime
choices and its default, and a task and a Paper writing session each export the
label for the runtime they ran on, so no surface maps a durable runtime id or
picks a default itself.

The browser may stage human drafts and render backend projections; it is not the
owner of authority, tasks, graph rules, provider authentication, watcher
delivery, or canonical state. Provider authentication stays native to the
execution account and is not owned by another RCP layer. Client-generated ids,
cached target selection, URL fragments, artifact messages, and provider output
cannot select a different project, conversation, branch, authorizer, or graph
target.

## Verification contracts

The durable journeys include [S03 graph views](../acceptance/S03-views-and-graph-controls.md),
[S19 draft preservation](../acceptance/S19-nothing-typed-is-lost.md),
[S30 desktop window lifecycle](../acceptance/S30-desktop-window-is-not-the-app.md),
[S31 Quit ownership](../acceptance/S31-quit-stops-what-it-started.md),
[S32 desktop previews](../acceptance/S32-artifacts-in-the-desktop-window.md),
[S53 truthful Runs projections](../acceptance/S53-truthful-attention-and-run-surfaces.md),
[S81 live canonical state](../acceptance/S81-live-canonical-state.md),
[S90 dictation](../acceptance/S90-desktop-chat-dictation.md),
[S105 multi-space desktop](../acceptance/S105-move-between-spaces-in-one-window.md),
[S109 current tabs](../acceptance/S109-tabs-stay-current-without-freezing.md),
[S125 branch merge](../acceptance/S125-auto-research-graph-branch-merge.md), and
[S128 team project provisioning](../acceptance/S128-provision-a-team-project-through-desktop-and-server-cli.md).
