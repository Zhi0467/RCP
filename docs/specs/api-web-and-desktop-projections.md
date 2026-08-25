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

Runs is the operational research-control surface, ordered **Running**, **Needs
action**, then **Completed**, first matching state winning. It contains
Seed/Refresh, bounded Experiments, Auto-research, and asserted open graph
Blockers—not ordinary chat or Paper coaching.

Experiment and Auto-research parents each expose one backend-decided health and
one separately labelled **Recommended next step**. Task status, phase, semantic
Experiment status, workers, and diagnostics remain supporting history rather
than competing primary states. A control is absent unless currently valid, and
no recommendation names an unavailable action.

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

The browser may stage human drafts and render backend projections; it is not the
owner of authority, tasks, graph rules, provider credentials, watcher delivery,
or canonical state. Client-generated ids, cached target selection, URL fragments,
artifact messages, and provider output cannot select a different project,
conversation, branch, authorizer, or graph target.

## Verification contracts

The durable journeys include [S03 graph views](../acceptance/S03-views-and-graph-controls.md),
[S19 draft preservation](../acceptance/S19-nothing-typed-is-lost.md),
[S30 desktop window lifecycle](../acceptance/S30-desktop-window-is-not-the-app.md),
[S31 Quit ownership](../acceptance/S31-quit-stops-what-it-started.md),
[S32 desktop previews](../acceptance/S32-artifacts-in-the-desktop-window.md),
[S53 truthful Runs projections](../acceptance/S53-truthful-attention-and-run-surfaces.md),
[S81 live canonical state](../acceptance/S81-live-canonical-state.md),
[S90 dictation](../acceptance/S90-desktop-chat-dictation.md),
[S109 current tabs](../acceptance/S109-tabs-stay-current-without-freezing.md), and
[S125 branch merge](../acceptance/S125-auto-research-graph-branch-merge.md).
