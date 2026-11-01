# Research Control Panel design

RCP turns agent-assisted research into one durable project record: a research
graph, a human authority queue, operational conversations and bounded episodes,
append-only provenance, and a human-controlled paper workspace.

## Product boundary

RCP owns research control around existing repositories, provider sessions,
experiment systems, and tools. It does not replace Git, schedulers, process
managers, notebooks, experiment trackers, or domain-native viewers. Repository
and provider side effects are real. Only an admitted Patch records their graph
meaning. Graph workflows do not roll external effects back.

The graph records what is believed and why, and controls work through Decisions,
Experiments, and observable Blockers. A Decision may be an Experiment input
without a Hypothesis attachment. Operational observations never silently become
project truth or human approval.

Human authorization bounds autonomous episodes and nightly consolidation.
Episode branches, human-dispatched merges, and optional repository worktrees have
separate owners. See [Auto-research](specs/auto-research-and-branch-merge.md) and
[consolidation](specs/graph-consolidation-and-lessons.md). Ordinary conversations
remain distinct from episode authority.

The paper workspace is human-controlled. Coach changes are reviewed or applied
under the chat's human-authorized mode. Artifacts, reports, comments, and versions
are reading and collaboration tools. They do not establish graph truth.
Their authority and lifetime belong to [paper and artifacts](specs/paper-artifacts-and-result-views.md).

Team trust and deployment belong to [server operations](specs/server-and-machine-operations.md).
Native compatibility belongs to the [API and desktop contract](specs/api-web-and-desktop-projections.md).
Provider login and execution belong to [providers](specs/providers-and-containment.md).
Page tools and voice compose ordinary controls with the member's authority. See
[native chat context](specs/conversations-episodes-and-watchers.md#native-chat-context).

## Cross-cutting invariants

The permanent [AGENTS.md registry](../AGENTS.md#stable-invariants) is the single
home for repository-wide invariants and their stable identifiers.

## Terminology

- A **Space** is one durable RCP authority domain with a SQLite control plane.
- A **Project** has one durable identity, a manifest, a repository set, and a
  canonical state repository.
- **Main** is the project graph visible in ordinary project views.
- A **Graph branch** has append-only history based on an immutable parent head.
  An episode lineage or a human owns it. It is separate from a Git or filesystem
  branch.
- A **Bulletin** is an operational board of hearsay for one graph target.
  It has no wake, steering, or authority effects.
- A **Patch** is an attributable, ordered list of typed semantic graph operations.
- A **Transition** is the manager-prepared atomic result of one mutation.
  It includes generated actions and lifecycle events.
- A **Proposal** is a request for human judgment on a graph change governed by
  [Proposal authority](specs/authority-and-proposals.md).
- A **Task** is one durable operation or recovery attempt, which may invoke a provider.
- A **Conversation** is a reusable scratch workspace for a native session.
  Its turns are explicitly labelled Discuss or Work.
- An **Artifact** is an RCP-owned file with SQLite metadata and immutable versions.
  It is never a graph object or a second answer channel.
- An **Episode** is a persisted parent for bounded Experiment control or
  Auto-research. It has one budget, a native-session binding, and a graceful
  Stop boundary.
- A **Watcher** is a durable observation that may admit a continuation.
  It does not prove external success.

## Module specifications

- [Graph, history, and transitions](specs/graph-history-and-transitions.md) covers
  ontology, claim-relative Evidence, typed operations, Patch logs, transition
  closure, replay, and graph heads.
- [Authority and Proposals](specs/authority-and-proposals.md) covers human and agent
  principals, action admission, protected beliefs, Proposal judgment, and
  attribution.
- [Providers and containment](specs/providers-and-containment.md) covers run
  capabilities, provider launches, exact project write scopes, remote execution,
  ingestion, skills, and durable task receipts.
- [Machines and compute](specs/machines-and-compute.md) covers base and project machines,
  scheduler readiness, OS-owned helper launches, shell-watcher actions, and
  durable receipts
  ([previous path](specs/compute-jobs.md)).
- [Conversations, episodes, and watchers](specs/conversations-episodes-and-watchers.md)
  covers Discuss and Work context, private agent threads and handoff, Experiment
  control, native-session continuity, watcher delivery, Stop, and reporting.
- [Auto-research and branch merge](specs/auto-research-and-branch-merge.md) covers
  orchestrator authority, budgets, child work, episode graph branches, and
  human-dispatched semantic merge.
- [Graph consolidation and memory](specs/graph-consolidation-and-lessons.md) covers
  the authorized nightly consolidation turns, their Inbox rows, per-project
  memory files, and private agent schedules.
- [Projects, spaces, and operations](specs/projects-spaces-and-operations.md) covers
  durable identity, team enrollment, membership, project homes, setup, caches,
  and process ownership.
- [Server and machine operations](specs/server-and-machine-operations.md) covers the
  installed team server, machine authority, version and update lifecycle,
  central checkouts, provisioning, transfer, and backup and restore.
- [API, Web, and desktop projections](specs/api-web-and-desktop-projections.md) covers
  mutation envelopes, current application surfaces, revision reconciliation,
  tabs, and shell lifecycle.
- [Paper, artifacts, and viewing](specs/paper-artifacts-and-result-views.md) covers
  human paper authorship, reviewed coaching, previews, reports, unified
  artifact interaction, and repository-file reading.
- [Interface and visual design](specs/interface-and-visual-design.md) covers the
  visual grammar, project shell, Research and Runs projections, node detail, DAG
  controls, composer, and the no-commentary-lines rule.

## Current exclusions

The supported team-server target excludes public-internet HTTPS, containers,
Linux desktop packages, a hosted service, and multi-server failover.

- Graph branches are episode branches or forks of exact heads, not free-form
  histories. There is no conflict viewer, repository rollback, orchestrator merge
  into main, frontend transition-rule engine, or user-authored rule language.
- Watchers wake on completion rather than intermediate output. Observation
  failures remain visible, as does the history of completed, stopped, or degraded
  work. There is no separate history-cleanup feature.
- Parallel repository work has no exclusive repository lease. Shared-checkout
  concurrency and explicit code integration are separate from graph branching.
- Human graph editing uses preview and Sync. Artifact selection supplies
  conversation context, not an Evidence-creation or human-judgment shortcut.
- Provider containment is not a provider-neutral hostile-process sandbox. There
  is no hard provider interruption or persistent provider daemon. Steering uses
  the existing ordinary chat attempt.
- [Agent mail](specs/auto-research-and-branch-merge.md#mail-and-lifecycle-notices)
  connects the orchestrator to its workers in a star. There is no peer or
  cross-episode mail. A member's Space thread may hand that member's request to
  the member's own project thread; that is not mail. Agents of different
  members, and board agents, share only through the Bulletin.
- [Restore](specs/server-and-machine-operations.md#backup-and-restore) requires its
  explicit safety procedure. Matching `space_id` is not a client rollback detector.

The core is general-purpose, with no research-field allowlist or speculative
supported-domain claims. Domain connectors and visualization may extend it.
[Glossary definitions and deterministic quality advice](specs/graph-history-and-transitions.md#typed-graph-operations)
are metadata and guidance, not scientific judgment.
