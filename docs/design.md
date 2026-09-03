# Research Control Panel design

RCP is a local research control panel for turning agent-assisted research into
one durable project record: a research graph, a human authority queue,
operational conversations and bounded episodes, append-only provenance, and a
human-authored paper introduction with a read-only writing coach.

The graph is both a research record and a control input. Epistemic structure
records what is believed and why; action structure records the Decisions and
Blockers that govern Experiments. RCP may dispatch and observe work, but it
never silently converts an operational observation into canonical truth or an
agent's scientific judgment into human approval.

## Product boundary

RCP owns the research-control layer around existing repositories, provider
sessions, experiment systems, and tools. It does not replace Git, schedulers,
process managers, notebooks, experiment trackers, or domain-native viewers.
Repository and provider side effects remain real operational effects; canonical
graph history records their research meaning only through an admitted Patch.

RCP schedules autonomous work only inside a human-authorized Auto-research
episode with a fixed operational invocation budget. Every such episode writes
research-graph changes to its persistent graph-only branch. Main stays editable,
and branch work reaches main only when a human dispatches the dedicated semantic
merge agent. Repository files and external effects are never branched or rolled
back by that graph workflow.

The paper introduction is human-authored and non-authoritative. Agent-created
artifacts and reports help a researcher read work; they do not become graph
truth. A chat artifact may be viewed, selected, questioned, and kept through one
shell. A Work revision remains a candidate until a human accepts or rejects it;
those interactions grant no graph authority. The backend
owns the viewer entrance, so ordinary server-served UI changes do not require a
matching native rebuild. The thinner native team entrance is a separate
boundary: before enrollment, token exchange, project-card read, or browser-cookie
installation, the source-built desktop and team server negotiate the highest
overlap in one inclusive integer protocol range. No overlap or a missing or
mismatched handshake answer refuses the connection and names both source commits
so the stale side can be updated from `origin/main`.

The confirmed first team deployment is one lab using one source-built RCP server
and desktop member clients. A dedicated Linux `rcp` account owns the control
plane and every server-local team checkout; an explicitly configured remote
execution account owns a team checkout on its SSH machine. Members remain
distinct RCP humans and may keep independent personal checkouts. RCP member
identity, process identity, SSH transport credentials, repository credentials,
and provider-native authentication are separate authorities. RCP selects and
readiness-checks a provider but never performs or stores its login; execution
uses whatever the configured operating-system account has authenticated
natively. The source server runs a built checkout of GitHub `main` as a
non-reloading service, and its commit and update lifecycle are managed by the
server CLI. "Source-built" does not make live development reload part of team
operation. The unfinished journeys that make this deployment usable remain
explicit pending acceptance work.

## Cross-cutting invariants

These are the repository-wide promises every module keeps. They are stated here
as principles; the permanent numbered identifiers that acceptance frontmatter,
source comments, and tests cite (`4b`, `10g`, and the rest) are registered in
[`AGENTS.md`](../AGENTS.md) under "Stable invariants" and are never renumbered.
The two lists decompose the same promises at different grain, so do not read a
number here.

- **Canonical history is append-only.** Main and Auto-research branch Patch
  logs are never edited or compacted. Materialized graph, research, glossary,
  Proposal, control, and branch outputs are derived and replaceable.
- **Graph changes have one typed channel.** Agents write one strict
  `patch.json` in RCP-owned scratch. Typed operations preserve the persisted
  JSON shape. RCP never parses graph authority from an answer, artifact,
  command trace, or repository edit.
- **One transition owns one mutation.** Human Sync, agent Apply, branch Apply,
  and branch merge all pass through the synchronous backend transition manager.
  Deterministic generated effects, validation, final graph, control, guidance,
  and events either commit as one revision or do not commit.
- **Replay records; it does not re-decide.** Replay applies the expanded
  operations already committed in each transition. It does not rerun historical
  rules or consult current users, memberships, provider settings, or SQLite
  operational state.
- **Humans retain the protected authority boundary.** Only humans set ordinary
  belief standing, resolve Proposals, change project truth membership, authorize
  a bounded episode, or dispatch a branch merge. Agents may assert new work and
  must propose changes to an existing ResearchQuestion or Hypothesis. The one
  deliberate Decision exception is the human-authorized Auto-research
  orchestrator: on its branch, and during its human-dispatched merge, it may
  choose Decisions directly. No agent may approve a Proposal.
- **Operational capability and graph authority are separate.** A task's fixed
  surface, provider profile, graph action, project target, current state, and
  budget all have to admit an effect. Work-like providers use native unattended
  write containment for the exact task stage and admitted project repository
  roots. This is an accidental-write guardrail for cooperative users, not a
  hostile-user security boundary or a read-confidentiality claim.
- **Canonical state has one home.** A project has one durable id, one home
  space, and one local or remote canonical state repository. Main and graph
  branch namespaces live inside that repository. Routes never write canonical
  files directly; state workspaces own locks, atomic publication, and remote
  recovery.
- **Operational state follows canonical events.** SQLite owns tasks, episodes,
  watchers, membership, and other mutable control-plane records. Stable
  transition event ids and target-specific watermarks reconcile a canonical
  commit into those projections idempotently after crashes.
- **One episode means one authority boundary and native session.** Experiment
  and Auto-research episodes pin their human authorization, graph target,
  provider session, execution host, exact reusable stage, and budget. Stop is a
  durable admission fence; every already-authorized turn settles honestly.
- **The browser renders one revision at a time.** A committed project response
  carries graph, graph-derived control, guidance validity, and head from the
  same transition. The client replaces that snapshot atomically. It never
  computes transition rules or splices a new graph into old control state.

## Terminology

- A **space** is one durable RCP authority domain and SQLite control plane.
- A **project** is one durable project identity, manifest, repository set, and
  canonical state repository.
- **Main** is the project graph visible in ordinary project views.
- A **graph branch** is an Auto-research episode's append-only graph history,
  based on one immutable main head. It is not a Git or filesystem branch.
- A **Patch** is an attributable ordered list of typed semantic graph operations.
- A **transition** is the manager-prepared atomic result of one initiating
  mutation, including generated actions and lifecycle events.
- A **Proposal** is an agent request for human judgment on a protected existing
  ResearchQuestion or Hypothesis.
- A **task** is one durable provider invocation or recovery attempt.
- A **conversation** is one reusable native-session scratch workspace containing
  explicitly labelled Discuss and Work turns.
- An **artifact** is a supported file produced by a task and owned by its
  conversation. It may remain temporary or be kept as a live file at the state
  repository root; it is never a graph object or a second answer channel.
- An **artifact revision candidate** is a validated Work output held beside its
  unchanged source until one human Accept or Reject disposition.
- An **episode** is the persisted parent for bounded Experiment control or
  Auto-research, with one operational budget, one native-session binding, and
  one graceful Stop boundary.
- A **watcher** is a durable observation that may admit a later continuation;
  it is not proof that external work succeeded.

## Documentation authority

Current sources have this precedence:

1. This file owns repository-wide product boundaries and cross-cutting
   invariants.
2. The applicable file in [`specs/`](specs/) owns current module behavior.
3. Active [`acceptance/`](acceptance/README.md) scenarios state selected
   observable promises and must agree with current design and specifications.
4. Active [`decisions/`](decisions/README.md) records explain rationale but do
   not override current design or specifications.
5. Active [`handoffs/`](handoffs/README.md) authorize and scope work not yet
   implemented. A human-confirmed, ready handoff need not be reconfirmed, but it
   may not silently change current design.
6. [`archive/`](archive/) is historical and non-authoritative.

[`server.md`](server.md) and [`desktop.md`](desktop.md) are operator and
developer guides. They own the exact procedure an operator runs and the native
build, verification, and release steps, so a scenario may cite one for a manual
path. They are subordinate to specifications and never define product behavior:
when a guide and a specification disagree, the specification wins and the guide
is the defect.

[`open-questions.md`](open-questions.md) records unresolved questions. A
contradiction among current sources is a documentation defect; do not choose a
winner by timestamp or silently implement around it.

## Module specifications

- [Graph, history, and transitions](specs/graph-history-and-transitions.md) —
  ontology, claim-relative Evidence, typed operations, Patch logs, transition
  closure, replay, and graph heads.
- [Authority and Proposals](specs/authority-and-proposals.md) — human and agent
  principals, action admission, protected beliefs, Proposal judgment, and
  attribution.
- [Providers and containment](specs/providers-and-containment.md) — run
  capabilities, provider launches, exact project write scopes, remote execution,
  ingestion, skills, and durable task receipts.
- [Conversations, episodes, and watchers](specs/conversations-episodes-and-watchers.md)
  — Discuss and Work context, Experiment control, native-session continuity,
  watcher delivery, Stop, and reporting.
- [Auto-research and branch merge](specs/auto-research-and-branch-merge.md) —
  orchestrator authority, budgets, child work, graph-only episode branches, and
  human-dispatched semantic merge.
- [Projects, spaces, and operations](specs/projects-spaces-and-operations.md) —
  durable identity, team enrollment, membership, project homes, setup, caches,
  and process ownership.
- [Server and machine operations](specs/server-and-machine-operations.md) — the
  source-built team server, machine authority, version and update lifecycle,
  central checkouts, provisioning, transfer, and backup and restore.
- [API, Web, and desktop projections](specs/api-web-and-desktop-projections.md) —
  mutation envelopes, current application surfaces, revision reconciliation,
  tabs, and shell lifecycle.
- [Paper, artifacts, and viewing](specs/paper-artifacts-and-result-views.md) —
  human paper authorship, read-only coaching, previews, reports, unified
  artifact interaction, and repository-file reading.
- [Interface and visual design](specs/interface-and-visual-design.md) — the
  visual grammar, project shell, Research and Runs projections, node detail, DAG
  controls, composer, and the no-commentary-lines rule.

## Current exclusions

RCP has no general graph-branching product, branch editor, conflict viewer,
repository rollback, orchestrator self-merge, frontend transition-rule engine,
user-authored rule language, provider-neutral hostile-process sandbox, direct
graph manipulation canvas, live output watcher, live provider interruption, or
peer-to-peer agent mail. Confirmed but unimplemented product journeys remain
explicitly `pending` in the active acceptance suite rather than being described
as current behavior here.
