# RCP — Blueprint v0.4

**Status:** design locked for v1, ready for implementation handoff
**Date:** 2026-07-29
**Supersedes:** `research-control-panel-blueprint-v0.3.md` (2026-07-27)
**Derived from:** `Fun — ChatGPT - Zhi.pdf` (17pp) + design interview 2026-07-26
+ design grilling 2026-07-27 (three rounds) + implementation review 2026-07-28

---

## 0. What changed in v0.4

**The 2026-07-29 implementation amendment bounds state transport and storage.**

- Seed, refresh, node chat, and project chat run on the machine that owns the
  canonical state repository. Cross-machine staging of `.research/` is
  forbidden. The read-only paper coach remains independently placeable.
- Human graph editing is a project-wide local draft. Wording, standing,
  proposal, and ambiguity actions do not touch canonical `.research/` until the
  human presses **Sync**. One Sync holds one state-workspace transaction,
  appends the resulting strict approval patches, materializes once, and
  publishes once. A no-op draft publishes nothing; a stale base revision is
  refused without a partial apply. The patches from one Sync become visible as
  one committed batch directory; an interrupted local write or SSH publication
  never exposes only a prefix of the human transaction.
- Transcript copies and session slices are explicitly disposable caches with
  time, count, and byte limits. Original provider logs remain external source
  material and are never deleted by RCP.
- Refresh receives a bounded recent-change routing delta, patch replay gains
  verified derived checkpoints, and operational task/session storage is
  retained under explicit limits.

**The `write_path` mode is deleted.** v0.3 specified two ways to get a patch out
of an agent — `protected` (emit the patch as the final chat message, app parses
it) and `direct` (agent appends the patch file itself under canonical
`.research/patches/`). Both are gone, replaced by one unnamed mechanism:

- RCP creates a **scratch folder** — a temporary directory on the execution
  machine, outside every repository and outside `.research/`.
- The agent is launched with its working directory on that folder, which is the
  only location it may write.
- It writes exactly one `patch.json` there. **That file is the only deliverable
  channel.** Nothing is parsed out of stdout or the final assistant message.
- RCP collects the file, validates it, and appends it through the history
  manager, exactly as `protected` mode always did.

Why: `protected` mode required the model to emit a large exact-schema JSON blob
as conversational output, which is the least reliable thing a coding agent does;
`direct` mode gave the agent a write grant inside the canonical state repo,
which needed the post-hoc audit path in §6.4 to stay safe. The scratch folder
gets the reliability of a file without the write grant. Validation is once
again unconditionally **before apply**. Rejected patches remain in the
append-only log as auditable evidence but contribute no state; what disappears
is the direct-mode-only post-hoc audit path and its special banner.

Three consequences worth stating separately:

- **Repositories are never copied.** v0.3 allowed a local run to snapshot remote
  Git history into a bounded local cache. That is deleted. A repository is a
  pointer `{alias, machine, host, path}`; when it lives on another machine the
  agent is told the host and path and reads it over SSH. There is no size
  envelope to exceed and no refusal to surface.
- **Materialized state is not copied either, when it need not be.** When the
  canonical state repository already lives on the execution machine, the agent
  reads `.research/` in place. Only conversation slices — RCP-derived artifacts
  that exist nowhere else — are staged, and only when the execution machine is
  not the machine that holds them.
- **Recovery is automatic.** A run that produced good work no longer loses it to
  a filename mismatch or a schema slip. See §6.4a.

## 0.1 What changed in v0.3

Seven amendments from the later 2026-07-27 grilling and implementation pass. They distinguish durable
project membership from per-run prompt focus, close the delivery boundary for
human-accepted guidance, add a deliberately human-authored paper surface, and
make the app's entry point and agent boundaries explicit.

| # | Change | Sections touched |
|---|---|---|
| B1 | **One global graph; two repository scopes.** The human-guarded project truth scope says which repositories belong to the project. A run scope selects a subset of their raw inputs for one invocation. The whole graph is always exposed and is never split by repository. | D7–D8, D12–D13, §5.3, §6.2–6.6, §8, §14 |
| B2 | **The accepted core is `research.md`, with an honest guarantee boundary.** One canonical file is injected into every RCP-launched agent. Independent Claude/Codex sessions receive no guarantee and no synchronized copies are written to member repositories. | D8, §6.1, §8, §9, §14 |
| B3 | **Trust is a UI projection.** A persistent picker offers Working, Accepted only, and Review views. Filtering never changes the graph or hides unresolved counts. | §10, §14 |
| B4 | **Each project gains a human-authored paper introduction.** The canonical Markdown file is editable only through the embedded editor within the RCP boundary, remains non-authoritative, starts from a six-part scaffold, and supports offline drafts with hash-guarded sync. | new D15, §6.1, new §11, §14 |
| B5 | **Writing help is coaching through native read-only sessions.** Claude/Codex sessions can be started or resumed, receive pointers rather than copied context, are pinned to their provider/model settings, and may prescribe editing actions but never generate replacement prose. | new D16, §11, §13–§14 |
| B6 | **Overview-first shell and explicit per-surface agent profiles.** Seed, refresh, node chat, project chat, and paper coach each receive provider/model/reasoning/machine defaults plus a non-widenable permission declaration. Every invocation may override operational settings. A dedicated DAG remains a projection of the same global graph. | D10–D12, §6.2, §8, §10, §13–§14 |
| B7 | **Background graph work is preemptible and inspectable.** Seed and refresh persist an operation receipt, native session and scratch-folder checkpoint, event trail, and estimated progress. Humans can pause, resume a checkpoint as a linked attempt, or retry cleanly after pause/failure/interruption. | D10, §10, §13–§14 |

The scope sentence to keep is: **the project graph and `research.md` are global;
run scope controls only which raw repository inputs are pointed to for that
invocation.** Adding or removing a repository from the project truth scope is a
human-authority change. Historical graph knowledge is not deleted when its
source repository later leaves the scope.

The paper sentence to keep is: **the human writes every word; the coach helps
the human see what to write.** The introduction may inform questions and
ambiguities, but it never silently becomes accepted scientific truth.

---

## 1. Purpose

### The problem

The bottleneck is **human comprehension bandwidth, not agent competence**.

Coding agents (Claude Code, Codex) run faster than a human can maintain a mental
model of the project. State is smeared across long, interleaved conversations.
The human has to reconstruct "what is actually going on" by re-reading chat, and
the agent's prose is dense with repo-local dialect — arm names, gate names,
variable names, config identifiers — that is unreadable without recent context.

The concrete failure this tool exists to prevent: an experiment fails repeatedly
for four unrelated reasons, each one gets patched and resubmitted under the same
label, and the human cannot tell from the conversation whether this is one
problem or four, whether it blocks everything downstream, or whether the
scientific question has quietly changed shape underneath it.

### The intended workflow

The human opens the control panel, sees within two minutes what the project is
asking, where it is, what changed, what is blocked, and what needs their
judgment. They click into a node, ask "explain what's going wrong," get a
readable answer, and approve or reject the small number of things that genuinely
require their authority.

They can also open the Paper surface and write the introduction themselves:
articulate the question, adjacent questions, literature, high-level method,
results, and why the work matters. A read-only coach helps expose weaknesses
without supplying the prose.

**The graph is a comprehension interface first, and a control surface second.**

### Non-goals for v1

This is explicitly **not** a job scheduler, an experiment tracker, or an
event-sourced control plane. The source PDF contained a second design along
those lines (SQLite event store, `researchctl` CLI, Slurm/Git collectors, gate
DAG, retry budgets). That design is **rejected for v1** — it solves
record-keeping, and the problem is comprehension.

The rejection is of collection *as the architecture*, not of collectors as a
convenience: v0.2 admits optional `sacct`/`git log` dumps that exist purely to
save the agent a round trip (D14). Nothing in the graph depends on them.

It is also not an AI paper-writing system. V1 edits only the human-owned
introduction surface and deliberately omits agent-authored prose, manuscript
generation, citation management, and submission workflow (D15–D16).

---

## 2. Scope boundary

| | v1 (this document) | v2 |
|---|---|---|
| Graph | agent-maintained, with direct human prose correction and approval | same |
| Node chat | explains, corrects the graph | dispatches work |
| Write-back | generated files the coding agents read | active dispatch |
| Paper introduction | human-authored Markdown + read-only coaching | broader paper workflow, if needed |
| Execution facts | agent-read, optionally fed by timestamped collector dumps | collectors scheduled and authoritative; graph trusts them over agent prose |
| History coverage | seed reads the full corpus; refresh defaults forward and records any backward read | — *(nothing deferred; see D13)* |
| Shipping form | local web app in a browser | desktop app (Tauri wrapper) |
| Character | **comprehension artifact** | **control panel** |

The v1/v2 line is one sentence: **v1 records understanding and human-authored
paper prose, but its agents never change code or execution; v2 dispatches work
that changes the research project.**

History access and repository membership are different boundaries. A graph run
exposes raw files only from the run truth scope and makes them read-only;
the whole project graph and `research.md` remain visible on every run. Inside a
selected repository, v1 does not pretend to constrain which historical records
the agent reads. It requires honest coverage instead: collector dumps carry the
timestamp they were observed at, and any patch citing history outside the
declared coverage must move the boundary (D13–D14).

---

## 3. Locked design decisions

Each was resolved during design. Rationale included because the implementer will
be tempted to reverse several of them.

### D1 — Conversation-derived, agent-native

The graph is derived from agent conversations by an agent. There is no
deterministic collection layer in v1.

### D2 — The agent maintains the graph; the human holds authority

The agent writes and refreshes the graph, including research questions,
hypotheses, and decisions. The human may directly correct a node's prose through
the detail UI. That edit is narrow: title and node-type-specific explanatory
fields only, never identity, standing, confidence, status, evidence references,
relations, attempts, or artifacts. It appends a human-authored patch through the
same history and validation path as every other change. Authorship and authority
remain explicit instead of making a wording correction depend on another agent.

D15 separately makes the paper introduction human-authored, human-edited, and
non-authoritative. Direct graph prose edits remain explicit human patches; they
do not revive a persistent `human_authored` flag on nodes.

Consequence: `human_authored: bool` from the source PDF is **deleted**. It is
always `False` at creation and therefore carries no information. It is replaced
by `standing` (D3).

### D3 — `standing`: three-state provenance on every node

```
asserted   — no current human judgment on this content
accepted   — human looked and agreed
contested  — human pushed back; agent owes a revision
```

Operationally, `asserted` means **there is no current human judgment on this
content**. It is not a claim that the node has never appeared in review: the
append-only history preserves earlier acceptance or contest. The human UI has
two judgments, Agree (`accepted`) and Disagree (`contested`). Selecting the
active judgment again withdraws it to `asserted`; this is a cleared judgment,
not a third judgment choice. An agent revision also returns changed content to
`asserted` unless it is the exact replay of an approved proposal.

The agent may create and edit ungated content freely; those writes land at
`asserted`. Gated content changes become proposals (D4), and an approved replay
preserves the human's resulting `accepted` standing. **There is no universal
write gate.** One would stall the graph behind human attention, which is the
scarce resource the tool exists to conserve.

Seeding never accepts nodes automatically. A human establishes initial trust
one node at a time: open the detail drawer, use node chat to gather context if
needed, then stage a judgment. Review and wording changes remain in the local
project draft until Sync. Sync writes strict human `approval` patches through
one canonical transaction; a standalone standing change does not need a
`Proposal`. Proposal rejection may mark related content contested. Beginning a
wording edit clears the draft judgment to `asserted`, because revised prose has
not yet received a human judgment. If evidence needs to be reinterpreted or
graph structure needs to change, node chat remains the evidence-reading
correction path.

`asserted` vs `accepted` must be **visually unmissable** in the UI. The failure
this prevents: a clean graph node laundering an unverified agent claim into
apparent fact. Chat wears its uncertainty on its face; a graph does not.
Silently corrupting the human's mental model is the one outcome strictly worse
than the status quo.

### D4 — Narrow approval gate

Approval is required for exactly these transitions:

1. `Hypothesis.status` change — the science actually moving
2. `Decision.status: open → decided` — the human's judgment being spent
3. `Evidence.validity → invalid | qualified` — retroactive invalidation
4. `Experiment.status → abandoned` — a pivot wearing a status update's clothes
5. Any `Blocker` of type `scientific` or `design` — design invalidity, not infra noise
6. **Any change to a node already at `accepted`** — general rule; the gate set
   grows exactly where attention has already been spent, and nowhere else
7. **Any change to project truth-repository membership** — changing what raw
   project material agents may consult is a human-authority decision

Explicitly **not** gated: experiment creation, attempts and retries, blockers of
type `implementation | infrastructure | data | unknown`, raw observations, all
routine status churn.

Rationale for narrowness: universal approval relocates the bottleneck rather
than removing it — the human would work a queue of agent-generated cards instead
of reading chat. Same volume, prettier container.

An agent that wants a gated transition applied does not apply it. It creates a
`Proposal` (§5.3), which is the object the attention view renders and the
approval patch consumes.

Direct human review of current content is not a gated transition. The human may
change `standing`, or directly edit the allowlisted prose fields. Beginning a
wording edit clears the draft standing to `asserted`; the edited wording can be
judged again before Sync. Domain statuses, evidence references, relations,
identity, and other semantic operations remain agent-authored and follow the
proposal rules above.

### D5 — Comprehensibility is structural, not prompted

Every gated card carries four **required schema fields** (§7). A project-level
**glossary** is a first-class, agent-maintained object. A **validator** checks
that identifier-shaped tokens in gated cards resolve to glossary entries or are
expanded inline, and flags failures with a visible banner. **Flag, never block** —
a stalled gate is worse than an imperfect card, but an unflagged bad card trains
the human to trust the format.

The source PDF's own example decision card fails this test ("A. Unfiltered first
epoch / B. Prior-banded curriculum" is unreadable cold). The design already has
the disease; prompting alone will not cure it, because the person who would
notice the decay is the person who by construction lacks the context to notice.

The same cold-read standard applies to every node, not only to approval cards.
A node's primary claim, question, observation, objective, or blocker description
must stand on its own when the drawer is opened without the surrounding graph:
it starts in ordinary language, expands necessary technical terms inline, names
the concrete project context, and uses enough complete sentences to explain why
the item matters. The glossary supplements that prose; it is not permission to
write identifier-heavy fragments. The detail drawer presents the primary claim
first, separates context from metadata, and surfaces matching glossary
definitions. Agent prompts carry this contract because agents author node prose;
the UI never invents or silently rewrites scientific content.

### D6 — Duplication bias, and what idempotence actually means

Entity resolution ("is this the same experiment as before?") is the agent's
central task and the place the graph silently rots.

- Node ids are **agent-authored readable slugs**, not UUIDs:
  `exp/arm-b-real-data-smoke`, `dec/first-epoch-difficulty`, `hyp/search-beats-value`.
  The agent must recognize its own prior work by reading the graph; a UUID is
  opaque to the thing that has to match against it.
- **No upsert operation.** `update_nodes` requires an existing id;
  `create_nodes` makes a new one. No op silently means "or create it if I got
  the id wrong."
- **Merges are explicit and non-destructive** — mark one superseded, keep both.
  Never delete.
- **When unsure, duplicate.** A duplicate is visible clutter the human can
  collapse in one click. A wrong merge is two different failures collapsed under
  one confident label — precisely the pathology the tool exists to cure, rebuilt
  inside the tool.
- Merges involving any `accepted` node, or nodes whose evidence points in
  different directions, are **refused and surfaced as an ambiguity**.

**Idempotence, stated precisely (amended in v0.2).** v0.1 claimed re-running
refresh over a processed window produces "zero net graph change," and attributed
that to content-derived slugs. That attribution was wrong: slugs are
*agent-authored*, not deterministic. The same window read twice may yield
`exp/arm-b-real-data-smoke` and `exp/arm-b-smoke-real-data`. The property is
therefore split:

- **Identity idempotence — hard requirement.** A re-run over an already-processed
  window creates **zero new nodes and zero new edges**, and changes no
  `standing`, no `status`, and no `validity`. This is the property that protects
  against rot, and it is produced by the agent seeing the current graph in
  context and re-asserting rather than re-creating — not by slug determinism.
- **Content idempotence — not required.** Prose fields (`current_summary`,
  `interpretation`, `rationale`) may churn on re-read. Do not test for byte
  equality of `graph.json` after a re-run; that test would fail on correct
  behavior and train everyone to ignore it.

Two mechanisms support the hard requirement:

1. The refresh prompt carries an explicit instruction to search the current
   graph for a covering node before emitting any `create_nodes` op (§8.1).
2. The validator **fuzzy-matches** each proposed new slug against existing slugs
   sharing its type prefix (token overlap / edit distance) and **flags** — never
   blocks, per D5 — `"possible duplicate of exp/…"`. Near-miss slugs surface for
   human attention instead of silently accumulating.

Useful side effect: **stable identity makes cursor precision optional.**
Re-reading an already-processed window re-asserts the same nodes and adds
nothing. This matters because refresh is an unconstrained agent call that may
stop halfway.

### D7 — Global graph + scoped raw locators, not a pipeline

The refresh agent is **not** a staged extraction pipeline. It is a normal Claude
Code / Codex invocation given the whole project graph, canonical `research.md`,
the read-only non-authoritative paper introduction, and an explicit **run truth scope**: a selected subset of the human-approved
project truth repositories whose raw locators and matching conversations enter
this invocation. Raw repositories outside the run scope are not injected into
the prompt or shared filesystem. Their previously derived graph knowledge
remains visible because there is one project graph, never a graph per repo.
Inside the run scope, the agent may read transcripts, repos, `git log`, `sacct`,
logs — whatever it judges necessary — plus a few convenience readers.

Explicitly rejected during design: declared refresh budgets, a mandatory
distiller stage, and an evidence-channel/lookup-channel taxonomy. The coding
agent already knows how to read large files without ingesting them whole, and it
already terminates on its own. Do not rebuild those affordances.

This decision is the reason D13 (full-corpus seeding) is consistent rather than
reckless: the same capability D7 trusts for refresh is what makes a full seed
tractable.

### D8 — One state repo; guarded project scope; per-run focus

- The manifest retains a registry of named `(alias, machine, path)` repository
  descriptors. Multiple entries may point to pre-existing checkouts of the same
  repository or distinct repositories belonging to the same paper project.
  Descriptors that have contributed to the graph are never deleted, so old
  `SourceRef` aliases remain resolvable.
- A separate **project truth scope** is the human-approved subset of registry
  aliases currently allowed to supply raw project evidence.
- Adding or removing a member is gated. An agent may create a `Proposal` whose
  stored op is `set_project_truth_scope`; adding a new alias carries its full
  descriptor. Only human approval applies it. No agent path can edit the
  manifest. Removal retains the descriptor and all historical graph knowledge,
  but prevents future raw access until the alias is approved again.
  Membership apply also maintains coverage mechanically: a newly approved alias
  enters `repositories_never_seen`; a removed unseen alias leaves that list;
  `repositories_seen` is historical and never shrinks.
- Every seed, refresh, and node-chat run carries an explicit
  `run_truth_scope`, a non-empty subset of current project members. Only those
  raw repository locators and matched conversations enter that invocation.
  Choosing a run scope is prompt construction, not project membership.
- The CLI and UI expose that selection per run. Omitting it uses
  `default_run_truth_scope`; an explicit empty scope is refused, and there is
  no implicit "all repositories" fallback. The exact run scope is recorded on
  the emitted patch.
- The **whole graph and canonical `research.md` are always exposed**, regardless
  of run scope. Nodes derived from another repository are not redacted or
  projected into a separate repo graph.
- Exactly one truth-repository alias is the **canonical state repo**.
  `<state-repo>/.research/` holds the patch log and all materialized state. It
  travels with that checkout, survives laptop loss, and gets git history for
  free. It remains a project truth-scope member in v1. State ownership does not
  automatically add raw state-repo content to a run scope; state transport is
  supplied separately. For
  `continual-RL-plasticity` (CRLP), the remote checkout is canonical.
- The local store is a **render cache** — fast, works offline, shows a visible
  "stale, last synced HH:MM" marker when it cannot reach the machine. Never a
  second source of truth.
- Human graph edits accumulate in a local project draft, stored outside
  `.research/` with the canonical graph revision they began from. **Sync** is
  the only graph-editing action that writes the draft to canonical
  `<state-repo>/.research/`. If the state machine is unreachable, Sync is
  refused and the draft remains local. Reconnection never auto-applies it; the
  human must press Sync again. If canonical revision moved, Sync reports a
  conflict rather than merging or partially applying the draft.

### D9 — Append-only patch log, managed by a history manager

Every refresh and chat correction **appends a patch**. One human Sync may append
several strict approval patches inside one state-workspace transaction, then
commits them as one append-only batch directory, materializes, and publishes
once. On SSH state, the batch directory is the remote commit point and is
exposed before the repairable materializations. A confirmed pre-commit failure
rolls the local mirror back; an uncertain commit is quarantined from replay; a
confirmed commit counts the Sync as successful and requires locked derived-file
repair before another canonical read or graph-agent launch. Startup performs the
same repair from the authoritative log. `graph.json` is *materialized* from the
log, never hand-written.

The human-authored paper is deliberately outside this log: the editor writes
`paper/introduction.md` atomically and Git records its text history (D15).
Turning human prose into graph ops would violate both authorship and
non-authority. Approved project truth-scope changes do remain patch-logged
because they alter agent access policy.

Four properties this buys, all of which the source PDF asked for separately:

1. `RefreshRecord` and revision history come free — the log *is* the history.
2. Append-only files merge cleanly in git; two writers rarely collide.
3. "What changed since I last looked" is a log slice, not a diff heuristic.
4. **Self-healing.** Storing the graph in the canonical state repo means any
   coding agent with write access can edit it mid-task. Since `graph.json` is
   derived, a stray hand-edit is overwritten on the next materialize. The only
   way to actually change the graph is to append a patch.

The history manager stays deliberately simple in v1: append, materialize, slice.
**No compaction in v1.**

### D10 — One graph-agent primitive, one background task lifecycle

Refresh, node chat, project chat, and seeding are the same graph-update
mechanism: *spawn an agent against this project with scoped context and receive
a patch*. Four prompt templates, one subsystem. Do not build four execution
paths.

Every invocation that launches an agent — Seed, Refresh, Node chat, Project
chat, and Paper coach — uses one server-owned `AgentTask` lifecycle. The launch
surface may close as soon as the task is accepted; source discovery, scratch
preparation, provider execution, validation, materialization, and coaching
continue outside the web request/event-loop path. The current status, exact
terminal failure, and bounded user-facing result are persisted in the app store
and recovered after navigation or page reload. Closing a chat or coach panel
never cancels its task. A single non-modal activity strip and inspector render
all five task kinds with the same progress, completion, failure, pause, resume,
retry, and dismissal language while the rest of the app remains usable.

The receipt also persists a bounded event trail, native provider session id,
local or remote scratch-folder root, phase, attempt lineage, and a frozen
duration estimate. Progress is explicitly estimated: use the median of up to 20
successful runs matching project, operation kind, provider, and model (reasoning
is ignored); otherwise use a five-minute refresh or ten-minute seed default.
The bar reaches 85% at the estimate, then approaches 99% until success alone
sets 100%.

Pause first marks the operation `pausing`, then terminates only its local and,
when applicable, remote provider process group. A normal RCP shutdown requests
the same pause. Resume creates a linked attempt pinned to the saved native
session and reattaches the stable scratch folder; it never mutates the prior
receipt. Seed and Refresh launch requests never accept a native session id:
Resume is the only entry point, and it verifies that the attempt lineage ends
at a sessionless RCP launch whose provider session was checkpointed by RCP.
Paused, interrupted, and failed attempts retain that scratch folder and any
patch text for inspection until age-based garbage collection. A linked clean
Retry starts without the native session and receives a fresh scratch folder;
it never mutates or reuses the failed attempt.
After an unclean restart, `queued`, `running`, or `pausing` receipts become
`interrupted` and offer Resume when a complete checkpoint exists, otherwise
Retry. No partial patch is materialized. A single-instance app-data lock prevents
overlap. An explicit `serve` command requests a normal shutdown from the owner,
waits for it to pause live work and release the lock, and only then takes over.

The paper coach (D16) reuses provider discovery, transport, and read-only
sandboxing, but it is not forced through the graph-patch contract. Its
`AgentTask` resumes a native provider session and persists coaching prose for
the paper UI. Sharing the lifecycle and launcher does not erase the different
output and persistence semantics: graph tasks require a validated file-backed
patch, while coaching tasks produce read-only advice.

### D11 — Node and project chat explain and can correct the graph

Node chat does three jobs:

1. **Explain** — "what's going wrong here," answered by actually reading
   transcripts, logs, and repo. The motivating use case.
2. **Correct evidence interpretation or structure** — the human says "that's a
   weakened hypothesis, not a rejected one"; the chat appends a patch (or, if
   the change is gated, a `Proposal`). Plain wording corrections use the direct
   node editor instead and do not invoke an agent.
3. **Decide** — the surface on which proposals get interrogated until
   understood, then approved. Without chat, an approval button is guessing.

Chat also supports review without forcing a correction: after asking for an
explanation, the human may stage Agree or Disagree in the same project draft.
Wording changes use **Edit node**; human standing changes remain UI-authored and
enter canonical history only through Sync, never through the chat agent.

Project chat provides the same read and graph-patch boundary without requiring
a selected node. It is for questions whose useful context spans the whole
project. Its transcript remains associated with the project; node-chat
transcripts remain associated with their nodes.

Hard boundary: **chat writes the graph, never the project.** No repo edits, no
commits, no job submission. That is v2.

App-chat transcripts are written to `.research/chat/` and treated as
**first-class conversation sources on the next refresh** — otherwise the most
information-dense exchanges about the project are the one channel the graph is
blind to.

### D12 — Agent profiles and per-invocation overrides (amended in v0.3)

Each of the five agent surfaces has a manifest profile containing provider,
model, reasoning level, and execution machine. Provider, model, and reasoning
are defaults that the launch UI may override for one invocation. The execution
machine is fixed to the canonical state machine for Seed, Refresh, Node chat,
and Project chat; manifest loading and per-invocation validation both enforce
that boundary. Paper coach remains independently placeable because it has no
graph or paper write path. Permission declarations are a separate fixed audit
contract, validated on manifest load and never overridden by a run.

**Where the agent runs.** Graph-agent surfaces execute beside canonical state:
local when the state repository is local, remote when it is remote. A state
repository on GPU-A with a graph agent on GPU-B is invalid. This removes
cross-machine copies of graph materializations and makes outbound provider
access on the canonical machine a prerequisite check. Repositories on other
machines remain read-only SSH pointers from that execution machine.

Note that no single machine necessarily sees all raw inputs in a selected run
scope: SSH-mode Claude Code transcripts live on the **laptop** (F1) while a
selected repo and cluster state may live on the **remote**. Whichever side the
agent runs on, the transport and convenience readers reach only the raw
locations declared by that run scope. The readers take a `machine` argument and hide
local-vs-SSH transport, because cursor-correct incremental JSONL reading over
SSH is the one genuinely fiddly part; everything else (`git log`, `sacct`, log
tails) uses ordinary scoped shell access, per D7.

**How the graph gets written.** There is one mechanism and it has no name — no
mode flag, no per-surface choice, nothing for the human to configure.

RCP creates a **scratch folder** on the execution machine: a temporary directory
that belongs to no repository and is not inside `.research/`. The agent is
launched with its working directory on that folder. It writes exactly one
`patch.json` there and nothing else. RCP then reads that file, validates it
(§6.4), and appends it to the canonical state repo through the history manager.

Three properties make this the whole design rather than an implementation
detail:

- **The deliverable is a file, never a message.** Nothing RCP consumes is parsed
  out of the model's conversational output or its final response. Asking a
  coding agent to emit a large exact-schema JSON object as chat is the least
  reliable operation available; asking it to write a file is among the most.
- **The agent has no canonical-state write path.** RCP trusts only the scratch
  `patch.json` and applies it after validation. Codex additionally enforces the
  repository boundary with its workspace sandbox. Claude is weaker:
  `acceptEdits` is required to write the patch and `--add-dir` grants technical
  edit access to directories supplied for reading, so its no-repository-write
  boundary is an explicit prompt contract rather than an OS-enforced guarantee.
  The UI and verification must state that difference honestly.
- **Nothing canonical is copied to the graph agent.** Run-scope repositories are
  read in place at their absolute paths, over SSH when they live on another
  machine. `.research/` is always read in place on its owning machine. Only
  disposable conversation slices may be staged.

Because the agent is not launched inside any repository, the launch prompt must
tell it to read each in-scope repository's `AGENTS.md` and `CLAUDE.md` if present
— the provider CLIs only auto-load those from their own working directory.

This governs agent-authored seed, refresh, and chat patches. Human UI approval
and standalone review patches always append through the history manager; no
agent path ever delegates human authority.

Paper coaching produces no patch and needs no scratch folder. It is launched
read-only and stays that way. In v1, native paper-session resume runs locally;
the wizard and launch UI surface that limitation rather than silently moving a
remote profile.

**Validation always runs before apply.** v0.3's `direct` mode needed a special
post-hoc audit path and banner because an agent could write straight into the
log. With that write grant gone, every patch now enters through the history
manager's one validator contract. Accepted and rejected patches are both
appended for auditability; deterministic replay applies only accepted patches.
The raw agent output is also persisted in the operation record before validation
and remains in the retained scratch folder on failure (§6.4a).

### D13 — Seeding reads the full corpus; lineage, not news (new in v0.2)

v0.1 bounded seeding to "a design doc plus a window of recent sessions,"
justified by F3: 46 MB will not fit in one context. That justification does not
survive contact with D7. It rules out *one context window*, not *one agent
invocation* — an invocation that skims, greps, spawns subagents per session, and
carries notes forward handles this corpus routinely, which is exactly the
capability D7 already relies on for refresh. v0.1 trusted that capability for
refresh and then declined to trust it for seeding.

So: **seeding reads everything inside its explicit run truth scope by default** —
all discovered conversations matched to the selected truth repositories, the
design doc, and those repositories — and produces revision 1. Declared truth
repositories outside the run scope are not injected as raw inputs, though any
knowledge already in the global graph remains visible.

Two constraints on it:

- **Lineage, not news.** A month of transcripts is mostly dead ends that were
  already superseded. A faithful reconstruction would bury current state under
  historical nodes the attention view then has to fight. The seed prompt
  therefore biases toward representing history *pre-collapsed*: abandoned
  threads land as `superseded` nodes and nested attempts, and only the current
  state of each thread gets prominence. This is what makes "why did we abandon
  arm X?" answerable from the graph — the arc is present, just not loud.
- **Scope and any further narrowing are recorded.** Revision 1 stores a
  **coverage boundary**: truth repositories seen or never seen, sessions read,
  sessions discovered but skipped, and the earliest timestamp the graph has
  seen. Selecting a subset of project truth repositories is normal routing,
  not an error, but the graph must not imply that excluded repositories informed
  it. Skipped history inside the selected run scope is likewise a fact about the
  graph. Both are surfaced in the project header and context assembly, so an
  agent says "that repository or era is outside graph coverage" instead of
  confabulating from partial state.

Every agent patch distinguishes availability from use: `run_truth_scope` says
what raw repositories were exposed, while `repositories_read` says which were
actually consumed. Only the latter moves an alias from
`repositories_never_seen` to `repositories_seen`.

Cursors land at the tail of every discovered session inside the seed run scope after
seeding, honestly, because everything in that scope was actually read. Sessions
matched only to unselected truth repositories receive no cursor until a later
run selects them.

**Forward-only is a norm, not a boundary (amended in v0.2).** An earlier draft
said refresh "never gains a full-history mode" and deferred recovery of skipped
history to a v2 `backfill` operation. That was unenforceable and therefore
false. Cursors are strings in a prompt; validation inspects the emitted patch,
not what the agent read. A run deliberately exposes full history inside the
selected run truth scope, so a reader-level time fence would mean rebuilding the
staged pipeline D7 exists to reject. This says nothing about raw repositories
outside the run scope, which a run does not expose at all.

What survives is the part that was actually load-bearing, and it is about
bookkeeping rather than access:

- Reading forward from cursors is the **default reading window**, and the reason
  is cost discipline — refresh must stay cheap enough to run often. It is stated
  as a strong instruction in the refresh prompt (§8.1) and nowhere claimed as a
  guarantee.
- An agent that *does* read earlier history must say so: any patch citing source
  records older than `coverage.earliest_timestamp` has to call `set_coverage`,
  which is therefore legal in `refresh` patches and not just `seed` (§6.3). The
  validator flags patches that cite pre-boundary history without moving the
  boundary (§6.4).

The failure this prevents is specific: without it, an agent wanders backward,
writes nodes from an old session, and the graph ends up *containing* knowledge
of an era it still *declares* it has never seen — after which context assembly
hands every future agent a coverage boundary that is quietly wrong.

**Consequence: `backfill` is not a v2 feature.** It degenerates to "a refresh
you asked to look further back," which the design already supports once
`set_coverage` is legal in refresh. One validator rule replaces a deferred
operation.

Accepted cost: a full seed is a long unattended run — plausibly 30–60+ minutes
and a substantial token spend on this corpus. It happens once per project, and
an incomplete graph taxes every session afterward.

### D14 — Collectors facilitate; they do not gate (new in v0.2)

v0.1 put deterministic collectors (Git, Slurm, artifact receipts) entirely out
of scope and modelled execution facts as v1: "whatever the agent reads and
reports" versus v2: "deterministic collectors." That framing was wrong in the
same way D13's was — it treated a facilitation gradient as a capability wall.
There is no reason to doubt the agent can find execution facts; `sacct`, `git
log`, and a log tail are ordinary shell reads it is already good at.

So collectors enter v1 as an **optional, additive facility**:

- A collector is a small scheduled job that writes a **mechanical, timestamped
  dump** to the canonical state repo's `.research/facts/` — `sacct --json`,
  `git log --format=…`, an artifact listing. Nothing interpretive.
- The refresh agent is prompted to read `.research/facts/` and to cite the dump
  it used in `artifact_refs`, so any claim about a job stays checkable against
  the raw output.
- **Non-load-bearing by construction.** If the directory is empty or stale, the
  agent does exactly what it does today: goes and looks itself. A project that
  never sets up a collector loses nothing but speed.

What collectors uniquely buy is **temporal, not epistemic**: a refresh-time
agent can only observe present state, however competent it is, and some
execution facts are perishable. `sacct` retention windows expire, scratch
directories get cleaned, logs rotate, and a job submitted and cancelled between
two refreshes may leave no trace either the agent or a later collector can find.
A collector running on a cadence observes moments no agent was present for. That
value materializes only if it actually runs on a timer — a collector invoked at
refresh time is just a slower way for the agent to run the same command.

Hard constraint, and the reason the dumps are timestamped: **every dump is an
observation at a time, never "current state."** The prompt must say so verbatim.
A stale dump read as live is exactly the silent-invalidation failure that makes
fallback paths dangerous in a research repo — the agent reports a week-old job
state as fact, and nothing in the graph shows the difference.

None of this makes v1 a scheduler (§1): a collector writes a log file, dispatches
nothing, and changes no part of the project. v2's version of this row is
narrower and genuinely deferred — collectors becoming *authoritative*, with the
graph trusting their output directly rather than through agent prose.

### D15 — The paper introduction is human-authored and non-authoritative (new in v0.3)

Each project corresponds, in the ordinary case, to one paper. The project gains
one canonical Markdown artifact:

```
<canonical-state-repo>/.research/paper/introduction.md
```

The embedded editor is the only RCP endpoint allowed to write this
file. Graph agents and the paper coach see it read-only; graph agents can write
only their one scratch-folder `patch.json` and cannot write `.research/paper/`.
This is the same honest boundary used for `research.md`: arbitrary agents
launched independently under the user's OS account are outside the guarantee.

On first creation, the editor seeds a **template, not a schema**:

```markdown
# Introduction

## What question we study

## What adjacent questions there are

## Literature review

## High-level methods

## Main results

## Why this deserves publication and communication to the community
```

The human may rename, reorder, combine, or delete every heading. There is no
schema validation and no forced completeness gate.

The introduction is a human reasoning surface, **not accepted project truth**.
Writing a sentence does not mutate graph nodes, change `standing`, or update
`research.md`. Agents may read it and point out disagreement with the graph;
any resulting graph change still appears as an ambiguity or gated proposal and
requires the ordinary authority flow. A tentative framing sentence must never
silently become a scientific instruction.

The repository file is canonical, appears in ordinary Git diffs, and gains Git
history when the project workflow commits it. The app data store holds only an
autosaved working buffer, base-file hash, undo state, and editor UI state.
Offline editing is allowed and visibly marked **Unsynced**. On reconnect,
the app writes automatically only when the canonical file still matches the
buffer's base hash. Otherwise it shows a conflict for human reconciliation and
never merges or overwrites automatically.

### D16 — The writing agent coaches; native CLIs own sessions (new in v0.3)

The writing agent is deliberately **not a ghostwriter**. It may quote the
human's existing text, identify missing logic, weak claims, adjacent questions,
literature gaps, unsupported novelty, and exact locations needing work. It may
ask targeted questions and prescribe editing actions. It may not generate
replacement prose, autocomplete, emit a paste-ready Markdown diff, modify the
document, or offer an Apply button. The human types every word that enters the
paper.

Writing calls are always user-invoked. A graph refresh may update a passive
"project understanding changed since this draft was reviewed" indicator, but
it never launches a writing call. Starting or resuming a writing session gives
the read-only CLI pointers to exactly:

- the current introduction (or a read-only snapshot of the unsynced local draft),
- the whole project graph,
- canonical `research.md`, and
- every repository in the guarded project truth scope.

It receives no raw conversation archive and no separate web-research mode.

Session persistence delegates to the native Claude and Codex CLIs. The paper UI
lists writing sessions it created and offers **Resume** or **New chat**. It does
not copy transcripts or implement its own compaction. The app data store keeps
only provider, native session id, execution machine, project association,
optional display title, created/last-resumed timestamps, and the introduction
hash plus graph revision last examined. Node chats remain associated with their
node; general chats remain associated with the project; discovered external
conversations do not enter the paper-session list automatically.

Provider, model, and reasoning setting are chosen at session creation and
pinned on resume. Changing any of them starts a new native session. The default
may be the configured lightweight Codex setting (for example 5.6 Luna at medium
reasoning) after its exact CLI identifier is verified. An unavailable model or
resume target fails visibly; there is no silent provider/model fallback. Every
resume prompt tells the agent to reread the current pointers before responding.

---

## 4. Verified environment facts

Established by direct inspection on 2026-07-26. These overturn assumptions in
the source PDF and must not be re-assumed away.

### F1 — Claude Code SSH sessions are stored on the *laptop*, not the remote

```
~/.claude/projects/ssh-<uuid>/<uuid>.jsonl
```

The directory name is a **session UUID**, not a path. The remote working
directory is recorded *inside* the file as `cwd`:

```
ssh-e3d37d30-…jsonl   cwd = /home/zhiwang/continual-RL-plasticity   9.4 MB
ssh-e7d53d4d-…jsonl   cwd = /home/zhiwang/continual-RL-plasticity   6.3 MB
ssh-a02a9381-…jsonl   cwd = /home/zhiwang/hypertree-or-whole-proof 16.5 MB
```

Local (non-SSH) sessions *are* path-keyed
(`-Users-zhiwang-research-learn/`), so the two modes differ.

**Consequences:**

- Conversation discovery is **by reading `cwd` out of each file**, never by
  directory name. This is an indexer, not a glob.
- Conversation sources for one project may live on **several machines at once**
  (laptop for SSH-mode Claude Code; remote for natively-run Claude Code and for
  Codex). Sources must be **unioned across machines, matched to truth-repository
  aliases, then filtered by the invocation's run truth scope**.
- The transcript records **no host identifier**. Two machines with the same
  path are indistinguishable from transcript content alone, so the **manifest
  must declare which hosts and truth-repository paths to scan**, and the indexer
  must record `(host_scanned, cwd)` as the composite key.
- Neither machine necessarily holds all raw inputs in a selected run truth scope,
  in either execution mode (D12).

### F2 — Claude Code JSONL record shape

Top-level keys observed:

```
type, uuid, parentUuid, sessionId, timestamp, cwd, gitBranch, version,
userType, message, toolUseResult, toolUseID, isSidechain, isCompactSummary,
compactMetadata, isMeta, level, slug, aiTitle, promptId, requestId,
errorDetails, isApiErrorMessage, stopReason, subtype, …
```

Notes for the reader tool:

- `uuid` / `parentUuid` form a chain → **the cursor is the `uuid` of the last
  processed record per session** (§6.6). Stable and cheap.
- `isSidechain: true` marks subagent turns — high-volume, often low-signal.
- `isCompactSummary` / `compactMetadata` mark context compaction. The transcript
  therefore **already contains model-written summaries**; the refresh and seed
  agents must be told to prefer primary turns over compaction summaries where
  both cover the same window. Compaction summaries are a legitimate *skim*
  layer during a full seed, but anything load-bearing gets read from primary
  turns.
- `toolUseResult` payloads dominate byte volume (file reads, bash stdout). The
  semantic content is a small fraction of the file.

### F3 — Scale

~46 MB of transcript across 14 SSH sessions, 2026-07-01 → 2026-07-26; a single
heavy day is one 16 MB file.

**Amended in v0.2:** v0.1 read this fact as "'read everything' is not available
at seeding time." That over-reads it. What the number rules out is ingesting the
corpus into a single context window; it does not rule out one agent invocation
reading it incrementally across many turns. Seeding does exactly that (D13).

### Environment facts verified during implementation (2026-07-27)

- Remote Codex sessions use `~/.codex/sessions/…`; native remote Claude sessions
  use path-keyed records under `~/.claude/projects/…`. The indexer matches both
  by embedded project `cwd` and indexes only matching records.
- The login/GPU node has outbound model-service network reachability.
- Codex CLI 0.145.0 and Claude Code 2.1.220 are installed and authenticated on
  the configured CRLP host. Both live on the interactive login-shell PATH, so
  remote provider probes and invocations deliberately use `bash -lic`.
- Provider readiness is still checked on every configured execution machine.
  D16 forbids silent model or provider fallback, and native Paper sessions
  remain local in v1 because remote resume needs persistent staging semantics.

---

## 5. Data model

### 5.1 Node types

Six, unchanged from the source PDF. **`Milestone` is deliberately excluded** —
it does not participate in the scientific reasoning loop, and adding it as a
first-class node duplicates experiments and creates ambiguity about which object
owns progress. Milestones may return in v2 as a *container above* experiments.

```python
ProjectNode = (
    ResearchQuestion | Hypothesis | Decision | Experiment | Evidence | Blocker
)
```

```python
@dataclass
class BaseNode:
    id: str                      # slug: "exp/arm-b-real-data-smoke"
    title: str                   # short, jargon-free
    standing: Literal["asserted", "accepted", "contested"] = "asserted"
    confidence: Literal["high", "medium", "low"] = "medium"
    created_rev: int
    updated_rev: int
    source_refs: list[SourceRef]
```

```python
@dataclass
class ResearchQuestion(BaseNode):
    question: str
    motivation: str
    scope: str
    status: Literal["open", "answered", "abandoned", "superseded"]

@dataclass
class Hypothesis(BaseNode):
    statement: str
    rationale: str
    predictions: list[str]
    status: Literal["proposed", "active", "supported",
                    "weakened", "rejected", "superseded"]

@dataclass
class Decision(BaseNode):
    question: str
    options: list[str]
    selected_option: str | None
    rationale: str | None
    consequences: list[str]
    status: Literal["open", "decided", "revisit", "superseded"]

@dataclass
class Experiment(BaseNode):
    objective: str
    design: str
    expected_outcomes: list[str]
    interpretation_rules: list[str]
    completion_criteria: list[str]
    status: Literal["proposed", "designing", "implementing", "debugging",
                    "running", "analyzing", "completed", "blocked",
                    "abandoned", "superseded"]
    attempts: list[ExperimentAttempt]     # nested, NOT graph nodes
    current_summary: str
    next_action: str | None

@dataclass
class Evidence(BaseNode):
    observation: str
    interpretation: str
    strength: Literal["diagnostic", "preliminary", "supporting", "confirmatory"]
    validity: Literal["valid", "qualified", "invalid", "superseded"]
    artifact_refs: list[str]

@dataclass
class Blocker(BaseNode):
    description: str
    blocker_type: Literal["scientific", "design", "data",
                          "implementation", "infrastructure", "unknown"]
    status: Literal["open", "resolved", "superseded"]
    resolution_condition: str
    recommended_action: str | None
```

`blocker_type` is **the routing rule**, not decoration: `scientific` and
`design` reach the human immediately; everything else stays silent forever.

### 5.2 Nested (non-node) records

```python
@dataclass
class ExperimentAttempt:
    id: str
    sequence: int
    purpose: str
    configuration: str
    status: Literal["planned", "submitted", "running", "failed",
                    "completed", "cancelled", "superseded"]
    job_refs: list[str]
    source_refs: list[SourceRef]
    outcome: str | None
    failure_reason: str | None
    started_at: datetime | None
    finished_at: datetime | None
```

Attempts stay **nested inside an experiment**, never top-level nodes. A failed
Slurm submission, a debugger run, a repaired smoke, and the final full run
remain one coherent lineage — that is the whole point.

```python
@dataclass
class SourceRef:
    machine: str          # machine where the conversation source was read
    truth_repository: str # declared alias whose path matched this session
    source: Literal["claude", "codex", "app_chat"]
    session_id: str
    record_uuid: str
    timestamp: datetime
    excerpt: str          # short, for the detail drawer
```

`paper/introduction.md` is deliberately **not** a `SourceRef.source`. It may be
cited by hash in `Ambiguity.artifact_refs` or described in a proposal card, but
it cannot serve as the source that directly creates or updates a scientific
node. That structural separation enforces D15's non-authority.

### 5.3 Side-car objects

**`Ambiguity` — first-class.** In the source PDF it was `ambiguities: list[str]`
on the patch, which means it evaporates on the next refresh. It must persist, be
addressable, be linkable, and be resolvable — otherwise "surface rather than
silently resolve" is not actually implementable.

```python
@dataclass
class Ambiguity:
    id: str                       # "amb/two-arm-b-smokes"
    question: str                 # jargon-free
    why_it_matters: str
    candidates: list[str]
    related_node_ids: list[str]
    artifact_refs: list[str]         # may cite paper/introduction.md by hash
    status: Literal["open", "resolved", "dismissed"]
    raised_rev: int
```

**`Proposal` — first-class (new in v0.2).** This is the object behind "gated
cards awaiting approval." v0.1 specified the rendering contract (§7) and the
attention-view surface (§10) but had nowhere to *store* a pending gated change,
and no op connecting "human clicked approve" to the field change that approval
authorizes. `Proposal` closes both.

```python
@dataclass
class Proposal:
    id: str                       # "prop/weaken-search-beats-value"
    card: GatedCard               # the four required fields (§7)
    ops: list[Op]                 # applied verbatim on approval
    related_node_ids: list[str]
    related_config_keys: list[str] # e.g. ["project_truth_scope"]
    base_rev: int                 # graph revision the ops were written against
    status: Literal["pending", "approved", "rejected", "withdrawn"]
    raised_rev: int
    resolved_rev: int | None
```

Lifecycle:

- A refresh or chat patch creates a `Proposal` (`create_proposals`). It may
  never apply the gated transition itself (D4).
- The attention view renders every `status == "pending"` proposal. No
  heuristics, no inference from node state.
- **Approve** → an approval patch appends the proposal's stored `ops` verbatim,
  plus `set_standing` → `accepted` on the affected nodes, and marks the proposal
  `approved`.
- **Reject** → the proposal is marked `rejected` and the target nodes go to
  `standing: contested`. The agent owes a revision (D3). This is the answer to
  v0.1's open question #7: rejection *is* how the human contests, and the
  rejection reason becomes the seed of the next node-chat exchange.
- **Withdraw** → a later refresh may find a proposal moot (the experiment was
  re-run, the evidence changed). Refresh and chat patches may mark a proposal
  `withdrawn`; they may never mark one `approved`.
- **Staleness.** If any node in `related_node_ids` or setting in
  `related_config_keys` has changed since `base_rev`, the approval is **refused**
  with "this proposal is stale; the underlying state changed," and the proposal
  is marked `withdrawn`. The agent owes a fresh one. A truth-scope proposal uses
  `related_config_keys = ["project_truth_scope"]`. Replaying ops written against
  state that has moved is exactly the silent corruption D3 exists to prevent.
  These fields are not trusted agent declarations: proposal creation requires
  `base_rev` to equal the current graph revision, derives the affected nodes and
  settings from `ops`, and rejects missing, extra, or duplicate dependencies.

A `Proposal` is required to authorize an agent-authored gated change to content.
It is **not** required for the human to review current content or directly edit
its allowlisted prose. A staged judgment becomes `set_standing` on Sync; a
direct edit becomes one narrowly validated `update_nodes` operation plus, when
needed, `set_standing: asserted` for the same node. Rejecting a substantive
proposal remains the UI path that marks related nodes contested (D2–D3, §6.3).

**`GlossaryTerm` — first-class, agent-maintained.** This is what makes gated
cards readable and is written from the actual config, not guessed.

```python
@dataclass
class GlossaryTerm:
    term: str                     # "arm_b_smoke", "prior-banded curriculum"
    plain_definition: str         # one sentence, no identifiers
    where_defined: str | None     # file path or config key
    updated_rev: int
```

**`CoverageBoundary` — the graph's account of what it has seen (new in v0.2).**
Written by the seed patch and **updated by any refresh that reads history older
than the current boundary** (D13). Read by context assembly and the project
header. This object is the enforceable half of history coverage: what the agent
reads inside its selected run truth scope is not time-constrained, but what the
graph claims to have read must be true.

```python
@dataclass
class CoverageBoundary:
    repositories_seen: list[str]      # aliases ever consumed; retained on removal
    repositories_never_seen: list[str] # current project members never consumed
    sessions_read: list[str]          # session keys
    sessions_skipped: list[str]       # discovered, deliberately not read
    earliest_timestamp: datetime      # earliest record the graph has seen
    note: str                         # why, in one human-readable line
```

### 5.4 Relations

```python
class Relation(str, Enum):
    HAS_HYPOTHESIS = "has_hypothesis"
    HAS_DECISION   = "has_decision"
    TESTS          = "tests"
    GOVERNED_BY    = "governed_by"
    PRODUCES       = "produces"
    BLOCKED_BY     = "blocked_by"
    SUPPORTS       = "supports"
    WEAKENS        = "weakens"
    CONTRADICTS    = "contradicts"
    REFUTES        = "refutes"
    INCONCLUSIVE   = "inconclusive"
    REQUIRES_DECISION = "requires_decision"
    SUPERSEDES     = "supersedes"
    DUPLICATE_OF   = "duplicate_of"      # duplication bias (D6)
```

Evidence→Hypothesis edges carry direction **per edge**, with an `explanation`.
The same observation may support one hypothesis and weaken another; a universal
per-node meaning would be wrong.

Typical shape:

```
ResearchQuestion --HAS_HYPOTHESIS--> Hypothesis
ResearchQuestion --HAS_DECISION----> Decision
Experiment --TESTS-----------------> Hypothesis
Experiment --GOVERNED_BY-----------> Decision
Experiment --PRODUCES--------------> Evidence
Experiment --BLOCKED_BY------------> Blocker
Evidence --SUPPORTS|WEAKENS|REFUTES-> Hypothesis
Blocker --REQUIRES_DECISION--------> Decision
```

---

## 6. Storage, patches, and history

### 6.1 Layout

```
<canonical-state-repo>/.research/
  manifest.toml          # project spec; truth membership is human-guarded
  patches/
    000001.json          # append-only; one file per patch
    batch-000002-000003-<id>/ # one atomic human Sync commit
      000002.json
      000003.json
    …
  graph.json             # MATERIALIZED — never hand-edited
  research.md            # GENERATED accepted core (§9)
  glossary.json          # materialized
  proposals.json         # materialized
  cursors.json           # per (truth repo, machine, source, session) → uuid
  coverage.json          # materialized; seed writes it, refresh may move it
  refresh.lock           # held for the duration of a refresh
  chat/
    <node-or-project>-<uuid>.jsonl # app chat transcripts; refresh reads these
  paper/
    introduction.md      # HUMAN-AUTHORED canonical paper introduction (§11)
  facts/                 # OPTIONAL collector dumps (D14); may be absent
    sacct-<iso8601>.json # mechanical, timestamped; observations at a time
    gitlog-<iso8601>.txt
```

Local cache mirrors `graph.json`, `research.md`, `glossary.json`,
`proposals.json`, and `coverage.json` under the app's data dir, with a sync
timestamp. The app data store also holds the paper editor's local draft buffer,
base hash, undo/UI state, and native writing-session registry. None of those
local records becomes a second canonical paper or graph.

### 6.2 Manifest

```toml
name = "continual-RL-plasticity"

[[machines]]
alias  = "gpu"
host   = "…"              # ssh target; omit for local

[[machines]]
alias  = "laptop"
host   = ""               # local

# Repository registry; used descriptors are retained even after scope removal.
[[repositories]]
alias   = "crlp-remote"
machine = "gpu"
path    = "/home/zhiwang/continual-RL-plasticity"

[[repositories]]
alias   = "crlp-laptop"
machine = "laptop"
path    = "/Users/zhiwang/research/continual-RL-plasticity"

# Human-guarded membership. Runs may select only a subset of these aliases.
[project]
truth_scope = ["crlp-remote", "crlp-laptop"]

# Exactly one repository owns .research/. CRLP's canonical state is remote.
[state]
repository = "crlp-remote"

# Default raw-input subset for agent runs; an invocation may override this with
# another non-empty subset of project truth-scope aliases.
[agent]
default_run_truth_scope = ["crlp-remote"]

# Conversation discovery: scan these roots, index by `cwd`, keep records whose
# cwd matches any registered repository path. Directory names are NOT
# trusted (F1).
[sources]
claude_roots = ["~/.claude/projects"]
codex_roots  = ["~/.codex/sessions"]

# Every agent surface has independent operational defaults (D12). The launch UI
# may override these four fields for one invocation.
[agent.seed]
provider   = "codex"
model      = ""
reasoning  = "medium"
run_on     = "gpu"

[agent.seed.permissions]
read_graph         = true
read_research_md   = true
read_introduction  = true
read_repositories  = "run_scope"
read_conversations = "run_scope"
write_graph_patch  = true
write_project_files = false
write_paper         = false

[agent.refresh]
provider   = "codex"
model      = ""
reasoning  = "medium"
run_on     = "gpu"

[agent.refresh.permissions]
read_graph         = true
read_research_md   = true
read_introduction  = true
read_repositories  = "run_scope"
read_conversations = "run_scope"
write_graph_patch  = true
write_project_files = false
write_paper         = false

[agent.node_chat]
provider   = "codex"
model      = ""
reasoning  = "medium"
run_on     = "gpu"

[agent.node_chat.permissions]
read_graph         = true
read_research_md   = true
read_introduction  = true
read_repositories  = "run_scope"
read_conversations = "run_scope"
write_graph_patch  = true
write_project_files = false
write_paper         = false

[agent.project_chat]
provider   = "codex"
model      = ""
reasoning  = "medium"
run_on     = "gpu"

[agent.project_chat.permissions]
read_graph         = true
read_research_md   = true
read_introduction  = true
read_repositories  = "run_scope"
read_conversations = "run_scope"
write_graph_patch  = true
write_project_files = false
write_paper         = false

[agent.paper_coach]
provider   = "codex"
model      = "gpt-5.6-luna" # illustrative; verify exact CLI id first
reasoning  = "medium"
run_on     = "laptop"

[agent.paper_coach.permissions]
read_graph         = true
read_research_md   = true
read_introduction  = true
read_repositories  = "project_scope"
read_conversations = "none"
write_graph_patch  = false
write_project_files = false
write_paper         = false
```

Discovery is by `cwd` match against registered repository paths, unioned over
machines. `host` is supplied by the manifest because the transcript does not
record it. Indexing may discover all registered locations. Every invocation
receives the whole graph and `research.md`; only raw locators and matched
sessions are filtered by `run_truth_scope`.

The permission tables are explicit for auditability, not knobs that grant new
capabilities. The loader compares each table with RCP's fixed contract for that
surface and rejects any widening or narrowing. Older manifests with global
`[execution]` and `[paper.coach]` sections may be expanded during migration, but
new manifests always write the five profiles above.

### 6.3 Patch envelope

```python
@dataclass
class Patch:
    revision: int
    kind: Literal["seed", "refresh", "chat", "approval"]
    author: Literal["agent", "human"]
    created_at: datetime
    summary: str                       # one line, jargon-free
    ops: list[Op]
    run_truth_scope: list[str]         # seed/refresh/chat; [] for UI approval
    repositories_read: list[str]       # actual raw aliases consumed; subset of run scope
    processed_cursors: dict[str, str]  # session key → last record uuid
    change_summary: list[str]          # human-readable, for "what changed"
```

Operations and where each is legal:

| Op | `seed` | `refresh` | `chat` | `approval` |
|---|:--:|:--:|:--:|:--:|
| `create_nodes` / `update_nodes` (no upsert, D6) | ✓ | ✓ | ✓ | ✓¹ |
| `create_edges` / `remove_edges` | ✓ | ✓ | ✓ | ✓¹ |
| `supersede_nodes` (non-destructive) | ✓ | ✓ | ✓ | ✓¹ |
| `merge_nodes` (non-destructive; marks `DUPLICATE_OF`) | ✓ | ✓ | ✓ | — |
| `create_ambiguities` / `resolve_ambiguities` | ✓ | ✓ | ✓ | — |
| `create_proposals` | ✓ | ✓ | ✓ | — |
| `resolve_proposals` | — | ✓² | ✓² | ✓ |
| `upsert_glossary` | ✓ | ✓ | ✓ | — |
| `set_coverage` | ✓ | ✓³ | — | — |
| `set_standing` | — | — | — | ✓ |
| `set_project_truth_scope` | — | — | — | ✓⁴ |

¹ As the replay of a `Proposal`'s stored `ops`, or one direct human prose edit
to a single node.
² `withdrawn` only. Never `approved`.
³ Required whenever the patch cites source records older than the current
coverage boundary (D13). This is what makes `backfill` an ordinary refresh
rather than a separate v2 operation.
⁴ Only as the replay of a human-approved `Proposal`; it may add a retained
repository descriptor and atomically updates membership, but may not delete a
used descriptor or remove the canonical state repo in v1. Coverage membership
lists update mechanically as described in D8.

Adding or removing any edge whose source or target is accepted content is a
gated semantic change. An agent patch must store it as a `Proposal`; only the
UI-authored approval replay may apply it. Agent output is also pinned to the
invocation's `kind` and `author = "agent"`, so it cannot forge that approval
patch shape.

An `approval` patch has one of three shapes:

1. **Proposal resolution:** replay the referenced proposal's stored ops
   verbatim and resolve it. For node-related proposals, set affected nodes to
   `accepted` or `contested`; a truth-scope proposal has no node standing.
2. **Standalone review:** one `set_standing` operation on a node in the current
   project draft, with no proposal and no semantic node operation.
3. **Direct prose edit:** one `update_nodes` operation containing one existing
   node and only that node type's allowlisted human-editable prose fields, plus
   at most one `set_standing` for that same node. It cannot change identity,
   confidence, status, evidence references, relations, attempts, artifacts, or
   project settings.

Only the human UI authors `set_standing`. Seed, refresh, and chat never accept
content, including during initial seeding.

### 6.4 Validation

Validation runs **before apply**: rejections abort the patch, while flags are
recorded and shown. There is one validator contract shared by admission and
deterministic replay (D12). A rejected patch is still appended to the canonical
log as evidence, but none of its operations are materialized. Independently,
the operation record persists the raw patch text before validation so malformed
output remains recoverable too.

### 6.4a Recovery

A run that reached a real conclusion must not lose it to a mechanical slip. When
the agent finishes, RCP walks this ladder and only stops for a human at the end
of it:

1. **The patch is not where it was asked to put it.** The whole scratch folder is
   searched for a JSON file that parses as a patch. Free, silent, and it retires
   the single most common historical failure.
2. **The patch is there but does not validate.** The exact validation errors are
   handed back to the *same live agent session*, which still holds the analysis,
   with an instruction to rewrite the file and change nothing else. Bounded to
   two rounds.
3. **There is no patch at all.** Same path, same bound.
4. **The patch is well formed but the graph rejects it.** Stop and surface it. A
   semantic disagreement is not something another round can resolve, and quietly
   retrying one would be a way to launder bad state into the graph.

Two supporting rules: the patch text is persisted **before** validation runs, so
a patch that fails is still recoverable evidence rather than a deleted artifact;
and the scratch folder of a failed run is **retained**, aged out on a retention
window rather than deleted at the moment of failure.

If that bounded same-session correction cannot produce an applicable patch, the
attempt ends in failure and offers an explicit linked **Retry**. Retry starts a
clean native session in a fresh scratch folder, carries forward only the prior
diagnostics, and leaves the failed attempt and its scratch artifacts unchanged
until retention cleanup.

**Reject:**

- An `approval` patch not authored by the human UI, or a seed/refresh/chat patch
  not authored by the agent
- `update_nodes` referencing a non-existent id
- `create_nodes` reusing an existing id
- Any gated transition (D4) in a patch of kind `seed`, `refresh`, or `chat`
- An `approval` patch that is neither a valid proposal resolution, standalone
  review, nor direct prose edit as defined in §6.3
- A standalone review containing anything other than one `set_standing`, or a
  direct prose edit containing anything other than one allowlisted single-node
  `update_nodes` operation and an optional same-node `set_standing`
- Approval of a `Proposal` whose `related_node_ids` or `related_config_keys`
  changed since its `base_rev` → refused as stale; proposal marked `withdrawn`
- `resolve_proposals` setting `approved` in a non-`approval` patch
- `merge_nodes` where either side is `accepted`, or where evidence directions
  conflict → converted to an `Ambiguity` instead
- Malformed slug (`<type-prefix>/<kebab-slug>`; prefixes `rq|hyp|dec|exp|ev|blk|amb|prop`)
- Any delete of a node, ambiguity, or proposal
- A seed, refresh, or chat patch whose `run_truth_scope` is empty, names a
  repository outside the guarded project truth scope, or introduces a new
  `SourceRef` from outside the run scope. Existing graph refs from other project
  repositories may be retained because the global graph is always visible.
- `repositories_read` naming an alias outside `run_truth_scope`, or a new
  `SourceRef` whose repository is absent from `repositories_read`
- `set_project_truth_scope` outside a human-approved proposal replay, with an
  unregistered alias lacking a complete descriptor, an unknown machine, an
  attempt to delete a descriptor already cited by the graph, or removal of the
  canonical state repository

**Flag (render a banner, do not block):**

- Gated card missing one of the four required fields (§7)
- Identifier-shaped token in a gated card that resolves to no glossary entry
  and is not expanded inline
- **New slug fuzzy-matching an existing slug of the same type prefix** —
  `"possible duplicate of exp/…"` (D6). Deliberately a flag: blocking here would
  punish the duplication bias the design depends on.
- **Patch cites source records older than `coverage.earliest_timestamp` without
  calling `set_coverage`** (D13) — *"this patch cites history the graph claims
  not to have seen."* The one mechanically checkable property that replaces the
  unenforceable "refresh may not read backward."
- **Execution claim resting on a stale collector dump** (D14) — a node whose
  only supporting `artifact_ref` is a `.research/facts/` dump timestamped before
  the previous refresh. Cheap to check, since the timestamp is in the filename,
  and it catches the one failure mode collectors introduce.

### 6.5 History manager

Deliberately minimal in v1:

- `append(patch) -> revision`
- `materialize() -> graph.json, research.md, glossary.json, proposals.json,
  coverage.json` — deterministic, fast, pure function of the log plus the
  current manifest base
- `slice(from_rev, to_rev) -> change list` — powers "what changed since I last
  looked"

**No compaction in v1.** A year of refreshes will be a lot of small files; that
is a v2 problem and should not shape v1's design.

### 6.6 Cursors, mechanically

Each conversation session is one append-only JSONL file that only ever grows at
the end, and every record carries a `uuid` (F2). A cursor is a bookmark:
`cursors.json` maps a session key to the `uuid` of the last record already
processed. The key includes the matched truth-repository alias so scope can be
applied without handing unrelated session locators to the agent.

```json
{
  "crlp-remote/laptop/claude/ssh-e3d37d30-…": "uuid-of-record-8412",
  "crlp-laptop/laptop/codex/01J9X…":          "uuid-of-record-221"
}
```

On refresh, the reader selects only session keys belonging to `run_truth_scope`,
scans each corresponding file, skips through and including the bookmarked
record, and hands the agent only what follows. After the patch applies, those
bookmarks advance to the last record read (`processed_cursors` on the patch).
This is what makes refresh mean *read the last two days in this scope*, not
*re-read the month*.

**Cursors are advisory.** This is worth stating flatly, because the design
elsewhere is easy to misread as claiming otherwise. A cursor is a pointer handed
to the agent in a prompt, not time-based context segregation. An agent that
decides to read further back inside its selected run truth scope can, and
sometimes should. Run truth scope still bounds raw inputs. The
property v1 enforces inside that scope is not *where in history the agent read* but
*whether the graph's coverage record matches it* (D13, §6.4).

Three consequences relied on elsewhere:

- Cursor precision is not safety-critical, because re-reading a processed window
  re-asserts existing nodes and adds nothing (D6). A refresh that dies halfway
  loses work, not correctness.
- Seeding sets the initial bookmarks for its selected run truth scope. Under D13 it
  reads everything in that scope, so every included cursor lands at its
  session's tail honestly. If a human further narrows sessions inside the
  scope, skipped sessions still get tail cursors — and the forfeiture is
  recorded in `coverage.json` rather than left as a silent hole. Repositories
  outside the scope receive no cursors.
- **Sessions discovered after revision 1** — because a machine was added to the
  manifest, a `[sources]` root was corrected, or a project repository entered a
  run scope for the first time — have no cursor. A session whose first record is *newer*
  than the coverage boundary is simply new: read it from the start, it is short.
  A session whose first record *predates* the boundary is recovered history, and
  gets the D13 treatment: either a tail cursor plus an entry in
  `sessions_skipped`, or a deliberate read that calls `set_coverage`. Without
  this rule a scope expansion or corrected manifest path silently triggers a
  full-history read of an entire repository on the next ordinary refresh.

### 6.7 Derived storage and replay bounds

RCP may cache transcript-derived material, but those copies are disposable and
bounded. Cleanup runs during startup and source indexing, never deletes or
modifies an original Claude/Codex JSONL, never evicts an item pinned by an active
task, and records access when a cache entry is actually used. An ingest task
opens its process-wide pin scope before indexing or context assembly and pins
each cached source, slice identity, and routing index before that path becomes
visible. Within each cache,
expiration runs first and deterministic least-recently-used eviction then
enforces count and byte caps:

| Derived store | Access TTL | Count cap | Byte cap |
|---|---:|---:|---:|
| Remote provider-JSONL copies | 30 days | 256 files | 1 GiB |
| Materialized session slices | 14 days | 512 slices | 512 MiB |

A cache miss reconstructs the artifact from its original source. If that source
is no longer reachable, context assembly reports an explicit source gap; it does
not silently substitute stale data. When a conversation source and the graph
agent are both on the canonical-state machine, the derived slice is built there
and read there rather than downloading a complete transcript to the laptop and
uploading the slice back.

The remaining stores have deliberately simpler policies:

- Successful scratch folders and graph-run outputs are removed immediately.
  Failed, paused, and interrupted artifacts are retained for 7 days.
- `AgentTask` rows keep the union of the latest 100 tasks per project and all
  tasks from the last 90 days. Active tasks and complete retry ancestry and
  descendants are always retained.
- Paper writing sessions keep the union of the latest 50 per project and all
  sessions from the last 180 days.
- Existing bounded event and receipt caps remain the authority for those rows.
- Canonical `.research/chat/` is project evidence, not a disposable cache. It
  receives no blind TTL; future compaction must preserve explicit provenance.
- Only collector-owned, timestamped dumps in `.research/facts/` receive the
  14-day collector retention policy. Unknown or manually placed files are left
  untouched.

Patch-log replay remains authoritative and append-only. To keep startup bounded,
RCP writes a derived checkpoint every 100 accepted revisions with the
materialized state and a hash of the complete patch prefix. Loading may begin at
the newest checkpoint whose prefix hash verifies, then replay the suffix. A
missing or invalid checkpoint falls back to full replay and can never block or
alter canonical recovery.

---

## 7. The comprehensibility contract

Applies to every `Proposal` card surfaced in the attention view.

### Required fields

```python
@dataclass
class GatedCard:
    cold_open: str        # the situation assuming the human has looked at
                          # NOTHING since last handoff. No identifiers.
    why_you_why_now: str  # what is stalled until they answer; what proceeds
                          # without them
    options: list[str]    # consequences in human terms: scientific truth for
                          # science gates, raw access for truth-scope gates;
                          # never only a config value or code path
    not_decided_by_this: str   # the boundary, so the card is not over-read
```

`GatedCard` is embedded in `Proposal` (§5.3), which is what makes it a stored
object rather than a rendering convention — and therefore something the
validator and the eval below can actually run against.

For `set_project_truth_scope`, the card must name the repository in plain
language, explain what evidence it can contribute, state what raw access will be
added or removed, and make clear that historical graph knowledge survives
removal. A naked alias/path diff is not comprehensible enough to approve.

### Glossary check

Extract identifier-shaped tokens from the card (snake_case, CamelCase,
`arm_b`-style, quoted paths, config keys). Each must resolve to a
`GlossaryTerm` or be expanded inline. Misses produce a banner:
*"this card may not be self-contained."*

### The eval

**A card is valid only if an agent with zero repo access can read it and
correctly state what is being asked.** Cheap to run, directly targets the stated
pain, and it is the only way drift gets caught — the human, by construction,
cannot catch it, because lacking context is exactly their condition.

Run this over a sample of stored proposals during development and keep it as a
regression check on prompt changes.

---

## 8. The graph-agent primitive

One graph-update mechanism (D10): spawn a Claude Code or Codex session against
a project with assembled context and receive a patch. Four prompt templates run
over it — seed, refresh, node chat, and project chat. Operational defaults come
from the surface profile and may be overridden for one invocation (D12). Paper
coaching reuses the provider launcher and read-only sandbox but follows the
native-session contract in §11 rather than this patch contract.

### Shared context assembly

- A scoped manifest projection: run-scope repository aliases, their machines
  and paths, matching conversation roots, canonical state transport, and
  execution config. Other project repository locators are omitted.
- The explicit `run_truth_scope` for this run
- The complete project-global `graph.json`
- Canonical `research.md`
- The current paper introduction pointer, marked human-authored,
  non-authoritative, and read-only (D15)
- Current `glossary.json`
- Current cursors
- Schema definition + the invariants below
- Open ambiguities
- Pending proposals
- **Coverage boundary** — so the agent knows what the graph has never seen
- **Available collector dumps** in `.research/facts/`, listed with their
  observation timestamps (D14). Absent on projects that run no collectors.
- For refresh only, a bounded `refresh_delta` routing summary described below.

The graph and `research.md` remain project-global so the agent can resolve
identity and retain human-accepted direction across run scopes. Run scope
controls only which raw repositories, conversation sessions, and execution
sources are injected now; it does not fork or redact the graph.

When the agent runs on the same remote machine as a matched conversation, its
read-only source path is passed directly; RCP must not download the session and
upload it back to that machine. Cross-machine sessions are staged, because a
slice is an RCP-derived artifact that exists nowhere else. Repositories are
never staged, snapshotted, or cached under any circumstances: a repository on
another machine is read over SSH from the host and path given in the prompt.
There is consequently no local-staging size envelope and no refusal to surface.

Conversation metadata in the prompt is a **routing index**, not another copy of
the transcript corpus. Each entry is compact: stable session key, provider,
machine, matched repository alias, time bounds, message/byte counts, cursor
state, and the path or reader needed to inspect it. The index is deterministically
ordered and capped; when it cannot fit, the prompt states the omitted count and
points the agent at the source index/reader rather than embedding progressively
larger per-session summaries.

### 8.1 Refresh prompt

Task: read forward from the cursors belonging to this run truth scope, update
the project-global graph, append one patch, and record both the exposed
`run_truth_scope` and actually consumed `repositories_read` on the patch.

`refresh_delta` is a bounded orientation aid over canonical graph history, not
a second state representation. It starts after the latest successfully applied
seed or refresh and prioritizes current contested nodes before recent standing
changes, direct human wording edits, graph changes from chat, and proposal
decisions. Standing entries carry both sides of the transition, including the
implicit return to `asserted` when an agent changes reviewed content. Entries
contain only revision, kind, target identifiers, and compact change
metadata—never full node prose or transcript text. The serialized delta is capped
at 50 entries and 16 KiB. If the current contested set or older events exceed
those bounds, it reports the omitted count and revision range rather than
blocking refresh. The full graph, patch log, and object history remain available
when an entry needs inspection.

Invariants handed to the agent verbatim:

- Every experiment connects to a hypothesis or a decision.
- Every piece of evidence connects to an experiment and a conversation source.
- Scientific decisions remain explicit `Decision` nodes.
- Implementation completion does **not** imply scientific completion.
- Failed and superseded attempts stay visible but must not dominate the view.
- Inferred claims carry source refs and confidence.
- **Ambiguities are surfaced, never silently resolved.**
- **Before emitting any `create_nodes` op, search the current graph for an
  existing node covering the same thing, and prefer re-asserting it via
  `update_nodes`.** Re-reading an already-processed window must produce zero new
  nodes and zero new edges (D6, identity idempotence).
- **When still unsure whether something is an existing node, create a
  duplicate.** Duplicate over merge, always.
- Gated transitions (D4) are never applied. Emit a `Proposal` carrying the card
  fields (§7) and the exact ops that approval should replay.
- The human-authored paper introduction is non-authoritative. Do not mutate a
  scientific node merely because the prose differs from the graph. Surface the
  disagreement as an `Ambiguity` with the introduction hash in `artifact_refs`,
  or as a gated proposal only when its graph change is independently justified
  by ordinary `SourceRef` evidence.
- Update `glossary.json` for every repo-local term used anywhere in a gated card,
  reading the actual config/code to define it.
- **Read forward from the cursors by default. You may read further back when the
  question requires it — but if you write anything sourced from records older
  than the coverage boundary, call `set_coverage` in the same patch.** The
  default exists so refresh stays cheap enough to run often, not to fence you
  out; what is not optional is that the graph's record of what it has seen stays
  accurate (D13).
- **Collector dumps in `.research/facts/`, if present, are observations at the
  timestamp in their filename — never current state.** Prefer them over
  re-deriving the same facts yourself, cite the dump in `artifact_refs`, and go
  look directly whenever the dump predates what you are describing (D14).

Convenience readers exposed (thin, optional — the agent may use its own tools):

- `read_claude_jsonl(machine, root, cwd_filter, from_uuid, …)`
- `read_codex_jsonl(machine, root, cwd_filter, from_uuid, …)`

They take a `machine` argument and hide local-vs-SSH transport, because
cursor-correct incremental reading over SSH is fiddly. The app constructs their
allowed roots and `cwd` filters from `run_truth_scope`. Everything else — `git log`,
`sacct`, log tails — uses scoped shell access. These readers exist for
ergonomics and cursor handling, **not as a time fence**. There is no enforced
history budget and no mandatory pre-filter inside the selected run scope (D7).

### 8.2 Seeding

A separate operation from refresh, run once per project, producing revision 1.

**Input set: the full corpus inside the explicit seed run truth scope by default**
(D13) — every discovered conversation matched to the selected repositories,
plus the design doc and those repositories. Unselected truth repositories are
not injected as raw inputs; the global graph remains available. The agent manages
its own reading strategy: subagents per session,
compaction summaries as a skim layer, primary turns for anything load-bearing
(F2). The seed patch records the selected repository scope; if the human further
narrows sessions inside it, `set_coverage` records what was skipped and why.
Because a complete seed consumes every raw repository in its run scope,
`repositories_read` equals `run_truth_scope` unless the seed is explicitly
narrowed and records that exception.

**Bias: lineage, not news.** Dead ends and abandoned threads are represented
pre-collapsed — `superseded` nodes and nested attempts — so the history is
present and answerable without competing for the attention view. Only the
current state of each thread gets prominence.

**Cursors land at the tail of every discovered session inside the seed run truth
scope.**

Every seeded node remains `asserted`. Seeding creates no bulk acceptance
proposal; initial trust is established later through the detail UI's staged
Agree/Disagree judgment, with node chat available when the human needs more
context (D3).

**Refresh defaults to walking forward from cursors**, and the reason is cost —
refresh has to stay cheap enough to run several times a day. Treat that as a
strong default in the prompt, not as a property of the system: it is
unenforceable, and v0.2 stopped pretending otherwise (D13). Recovering skipped
history is therefore not a separate operation waiting in v2 — it is a refresh
pointed further back, which must call `set_coverage` so the boundary moves with
it (§6.3, §6.4).

### 8.3 Node chat prompt

Same primitive, different task: answer the human's question about a specific
node, with the project-global graph and `research.md` plus full raw read access
inside the selected run truth scope and graph-patch-only write access (D11).
Gated changes surface as proposals, same as refresh. Explanation alone changes
nothing; after gathering context the human may independently Accept the current
content or edit its wording directly in the detail UI. Chat answers are
persisted on the shared background task and rendered in the conversation UI
whenever it is open; closing that UI has no lifecycle effect. Its transcript is
written to canonical `.research/chat/` for the next refresh to consume.

---

## 9. Accepted research write-back

**Exactly one graph-to-agent direction write-back exists in v1, and it is a
file.** The separately human-authored introduction is not a graph write-back.

`<canonical-state-repo>/.research/research.md` is generated whenever canonical
graph state commits. Agent patches commit through their automatic durable run
path; human review, prose editing, proposal decisions, and ambiguity actions
commit only when the human presses **Sync**. Local draft actions never write
`.research/` and therefore never rewrite `research.md`. Before the human agrees
with anything after seeding, it is intentionally empty. It holds:

- Accepted research questions and scope
- Accepted hypotheses with their current scientific status
- Accepted decided decisions with selected option and rationale
- Accepted `Decision` nodes that remain **open**, explicitly marked unresolved

Nothing else. The control panel explicitly injects the canonical file into
**every agent it launches**, regardless of run truth scope. Other project
repositories receive no generated copies that could drift, and v1 does not
modify their `AGENTS.md` or `CLAUDE.md` files to simulate a global guarantee.

Independent Claude/Codex sessions launched outside the control panel receive no
guarantee. Enforcing that would require modifying every repository or taking
control of every launcher, so v1 makes no such claim. A repo-local pointer may
be added manually as a convenience, never as a synchronized write-back system.

Why a file rather than injecting into a live session: sessions end, several may
run at once, and there is no durable API for writing into another agent's
conversation. A file survives session boundaries, works identically for both
agents, and is diffable in git.

**`accepted` is the thing that flows back.** This is what makes approval
meaningful rather than ceremonial: it is the channel through which the human's
judgment reaches the work. It also retroactively justifies the narrow gate set —
those transitions are gated precisely because they are the ones that change what
the coding agents are told.

Stakes escalation to keep in view: the graph is now a **source of instruction**,
so a wrong `accepted` node propagates into real work on real GPUs. Mitigations:
only `accepted` flows; `research.md` is small enough to read in thirty seconds;
it is a plain file in git with visible history; and proposal staleness checks
(§5.3) prevent approving a change written against a graph that has moved.

---

## 10. UI specification

Rendering model: **Overview first, then purpose-built projections.** Structured
text remains the default re-entry surface because it answers "where am I"
faster than a diagram. A dedicated deterministic DAG is available as a
secondary projection when relations matter. It renders the same project-global
graph, is never a repository graph or a second source of truth, and may use the
same trust-view filter as other graph projections. It must avoid a force-directed
hairball and must not become the default landing page.

Visual quality is an explicit requirement, not a nicety. The attention view's
entire job is visual hierarchy telling the human where to look.

The interface communicates state through hierarchy, shape, color, motion, and
control treatment—not explanatory subtitles or little helper comments beneath
controls. A compact icon, count, disabled state, or deliberate visual grouping
is preferred to persistent instructional prose. Error and conflict messages
remain explicit because they report an actual condition rather than narrating
the interface.

### Project index and add-project wizard

RCP opens at a persistent project index. **Add project** is a dedicated wizard,
not a free-form path box. It explains the model while collecting:

1. the paper-project name and first local or SSH repository;
2. every repository in the guarded project truth scope, which member owns
   canonical state, and which members are the default raw-input subset;
3. independent profiles for seed, refresh, node chat, project chat, and paper
   coach: provider, model, and reasoning level; the four graph surfaces show the
   canonical-state machine as fixed while paper coach may select a machine;
   each surface's fixed permission contract remains visible;
4. a read-only preflight and explicit final confirmation.

The wizard displays a live boundary ledger throughout: canonical state, the
single global graph, default raw prompt inputs, and all five agent profiles. SSH
paths are always paired with their host, so a path on another machine is never
presented as a laptop path. Provider readiness is checked on each selected
machine before confirmation.

Preflight may test directory existence and writability, read an existing
manifest, and inspect provider installation/authentication. It **must not**
create `.research/`, modify a manifest, or initialize remote state. If a valid
manifest already exists at the selected canonical repository, the final action
connects it exactly as written and ignores conflicting draft fields; it never
overwrites or relabels it. Otherwise, only the final checked confirmation may
create `.research/manifest.toml`. A remote confirmation names the exact SSH
write destination.

After initialization, a dedicated **Settings** projection persists
`default_run_truth_scope` and the provider, model, reasoning, and execution
machine for all five agent surfaces. The permission declarations remain
display-only and are regenerated from the fixed surface contracts on save. A
remote project publishes the updated manifest under the canonical state lock
and refreshes its local bootstrap copy; per-invocation controls remain temporary
overrides. Repository descriptors, canonical state ownership, and guarded
project truth membership are shown separately so they are not mistaken for
ordinary agent defaults.

### Repository controls — membership is not run focus

Project settings show the guarded **project truth scope**. Adding or removing a
repository is a human-approved configuration change and remains visible in
history. An agent may prepare the proposal but cannot apply it.

Each Seed, Refresh, Node chat, or Project chat action separately exposes a **run
truth scope** picker containing only current project members. It changes
prompt/raw-filesystem construction for that invocation and nothing else. The
whole graph and `research.md` remain present. The UI never labels this picker as
a graph filter.

Every Seed, Refresh, Node chat, Project chat, and Paper coach launch surface
also exposes its effective provider, model, reasoning level, execution machine,
readiness, and read/write contract. Operational choices apply to
that invocation; permission flags are display-only. Once a resumable chat has
started, its pinned choices replace the editable controls. The configuration
box is collapsible on all five surfaces. Node chat, Project chat, and Paper
coach keep the conversation as the dominant content instead of permanently
placing launch configuration or session history beside it.

### Trust-view picker — a projection, never a mutation

A persistent picker filters what the UI projects from the one global graph:

- **Working** *(default)* — accepted research question/hypotheses/decisions form
  the pinned research baseline; newer asserted research changes appear beside
  it as unreviewed updates. Fresh asserted execution state, blockers, and next
  actions remain visible with `asserted, as of …` labels because requiring human
  acceptance for routine churn would make the screen stale.
- **Accepted only** — shows only human-accepted graph content. Accepted open
  decisions remain visible and explicitly unresolved.
- **Review** — emphasizes asserted and contested nodes, pending proposals, and
  ambiguities that need attention, while retaining enough accepted context to
  understand them.

A contested node never drives a headline, but it remains visible in the Working
graph with unmissable contested styling so a request for correction cannot make
its target disappear. When no accepted baseline exists, Working may use asserted
research content, but the entire section is visibly marked unreviewed. The
selected preset persists per project/browser. Counts for hidden asserted,
ambiguous, or proposed items remain visible, so a filter cannot make unresolved
work silently disappear.

The picker changes no graph state, standing, agent prompt, run truth scope, or
`research.md`. Detail drawers can always reveal the complete object history.

### Project shell and Overview — the default

The compact project header keeps only the project name/status, **Activity**,
**Refresh**, and **Project chat**, followed by horizontal navigation: **Overview**, **Requires
you**, **Research**, **DAG**, **Runs**, **Glossary**, and **Paper**. Only
one panel is visible at a time. The trust-view picker appears on graph
projections instead of occupying every page.

Overview is the default landing page. It presents six clean, clickable re-entry
questions rather than a persistent sidebar or dashboard grid:

1. What are we asking?
2. Where are we?
3. What changed?
4. What is blocked?
5. What needs you?
6. What happens next?

Compact status metadata such as last refresh, unreviewed count, unseen project
repositories, coverage boundary, and paper sync belongs inside the relevant
answer or destination panel, not in a permanent attention rail.

### Requires You view — the contained attention surface

This is a dedicated page, never a persistent right-side rail. It contains only:

- **Pending proposals** — every `Proposal` with `status == "pending"`, rendered
  per §7 with the self-containment banner when flagged. Approve replays the
  stored ops; reject sets the target `contested` (§5.3).
- Open blockers of type `scientific` / `design`
- Open ambiguities
- **Blocking rollup** — computed, ranked by consequence:
  *"blocks 5 downstream items"*. This recovers the one thing a canvas would have
  shown, and does it better, because text can rank and a diagram cannot.
- Recommended next action
- Changes since last refresh

### Research view — question-centered paths

```
Question
├── Hypothesis or decision
│   └── Experiment
│       └── Evidence
└── Subquestion
    └── …
```

This is what gets read after time away from the project. It renders connected
research paths beginning at a question and proceeding by semantic stage through
hypotheses or decisions, experiments, and evidence. It is not a flat ledger of
every graph node. Records that cannot be placed on a question-centered path
remain accessible in an explicitly separate **Unconnected records** section so
missing relations stay visible without obscuring the research argument.

### Runs view

Runs combines RCP agent tasks with research experiments in one operational
projection. Failed and paused work and open blockers appear first, before
healthy or completed activity. Each retry nests under its logical root rather
than appearing as an independent top-level run, whether the item came from an
agent task or an experiment record.

Everything in this panel is **as-of**, never implicitly live: at best it reflects
the last refresh, and where a claim rests on a collector dump it reflects that
dump's timestamp (D14). Show the truthful as-of time next to the heading,
derived from the underlying observations rather than the page-render time. A
stale view that looks live is the execution-facts version of the laundering
problem D3 exists to prevent.

### DAG view — a bounded interactive projection

The DAG is embedded in the project page, with a native fullscreen control for
focused inspection. Entering or leaving fullscreen changes only presentation;
it does not change layout, filters, pins, focus, or project truth. Node details
remain openable and visible above the canvas in fullscreen; fullscreen is not a
graph-only mode that hides the selected node's information.

Touchpad pinch gestures zoom the DAG around the gesture's focal point, so the
content under the fingers stays anchored while scale changes. Ordinary
two-finger or wheel scrolling is not reinterpreted as zoom: it follows normal
nested-scroll chaining, panning the DAG while movement remains in that direction
and handing unused delta to the project page at the corresponding canvas
boundary. The canvas must not unconditionally suppress page scrolling. Zoom is
presentation-only and preserves node dragging and inspection, fullscreen, pins,
filters, and project truth.

Auto-layout positions define an initial arrangement, not the edge of the
interactive plane. The canvas reserves generous drag room beyond the laid-out
node envelope in every direction so a human can pull nodes apart without
immediately hitting a pan or placement boundary. The repulsion control is
behavioral, not decorative: moving between its extremes must visibly change
node separation on the same graph rather than merely changing a label or stored
number.

The stable DAG control is labeled **Research flow**. It fixes horizontal columns
by semantic stage—question, hypotheses or decisions, experiments, then
evidence—regardless of the source-to-target direction of stored relation arrows.
Edges retain their real direction when rendered; an arrow that points against
the research-stage order does not move either endpoint into the wrong column.

The DAG toolbar provides explicit **Brighten all** and **Dim all** actions. Node
focus may still brighten a selected one-hop neighborhood as described below.
Pinning is independent of brightness: the bulk action is labeled **Release all
pins**, and each pinned node exposes its active pin as a control that releases
that node alone when clicked.

### Detail drawer — on click of any object

- Full description
- Visually explicit `standing`: asserted, accepted, or contested
- Two visual judgment controls: **Agree** and **Disagree**. Clicking the active
  judgment again withdraws it to `asserted`; asserted has no third judgment
  button. **Edit node** opens a compact in-place form for title and the node
  type's explanatory prose fields and clears the draft judgment to asserted.
  Completing the form stages its changes and closes it. No agent task, canonical
  write, or chat message is created.
- Incoming and outgoing relations. The Relations control opens the DAG centered
  on this node, brightens it and every directly connected node, dims unrelated
  nodes and edges, and provides an explicit clear-focus action.
- Status history
- Source conversation excerpts (from `SourceRef`)
- Linked artifacts
- Revision that last changed it
- Related proposals, including resolved ones (why this node is `contested`)
- **Node chat panel** (§8.3)

The project shell owns one compact **Sync** control whose visual state and count
cover all staged node judgments, prose edits, proposal decisions, and ambiguity
actions. The project draft persists locally across navigation and restart. Sync
submits its base revision and the complete draft through one canonical
state-workspace transaction, materializes once, and publishes once; success
clears the draft. A no-op causes no canonical write. Unreachable canonical state
keeps the draft local; stale canonical state yields a visible conflict and no
partial apply. This contract is identical when the canonical repository is
local or remote—local `.research/` is still canonical state, never the draft.
Scientific status, evidence interpretation, and graph-structure changes still
route to node chat. Plain node prose is edited directly in the drawer.

### The default-screen test

Internally the project may hold hundreds of nodes. The default screen must still
answer only six things:

1. What are we asking?
2. Where are we?
3. What changed?
4. What is blocked?
5. What needs you?
6. What happens next?

> The graph preserves the project's semantics; the UI preserves the human's
> attention.

---

## 11. Paper workspace

### 11.1 Embedded Markdown editor

The project has a **Paper** surface containing an embedded Markdown editor for
the canonical introduction plus a writing-coach panel. Creating the document
once inserts the six headings in D15. From then on they are ordinary Markdown;
the app does not parse them into required fields or prevent structural changes.
The editor and coach are separated by a draggable divider so the human chooses
their relative width; the surface does not impose a fixed ratio. The document
begins with the authored Markdown itself, without a synthetic canonical-title
or `.research/paper/introduction.md` path banner above it.

Creation is local-first: pressing **Create** writes the starter template to the
app-store draft buffer and shows the editor immediately. Canonical workspace
refresh and publication happen on the ordinary autosave path and may remain
visibly Unsynced without blocking typing. An autosave response updates only the
paper snapshot; it never reloads the entire project or replaces a newer in-memory
buffer with stale canonical content.

The editor writes through one human-initiated app endpoint. It uses atomic
file replacement on the canonical state repo and never routes prose through the
agent patch log. Git supplies diffs and later committed history; the app does
not auto-commit. The app data store supplies continuous local autosave, undo,
cursor/selection state, and recovery.

The editor shows exactly one sync state:

- **Not created** — no canonical introduction or local draft exists yet.
  **Create** initializes the local buffer from the starter template; the first
  successful sync creates the canonical file.
- **Synced** — local buffer hash equals canonical file hash.
- **Unsynced** — canonical repo is unreachable or a local save is in flight.
- **Conflict** — canonical content changed since the buffer's recorded base
  hash; automatic sync stops until the human reconciles it.

Unsynced prose may be supplied to a coach as a read-only temporary snapshot.
It is never presented as canonical to graph refresh, `research.md`, or other
agents.

### 11.2 Non-authority and alignment

The paper has no `standing` field because it is already human-authored. It also
has no authority over the graph. The UI records the graph revision and
`research.md` hash last examined by each writing session and may display:

> Project understanding changed since this draft was reviewed.

This indicator is mechanical and launches nothing. Refresh may surface a paper
versus graph disagreement as an `Ambiguity` or gated proposal, but it may not
rewrite the paper or directly treat its prose as evidence for a scientific
status change. The human resolves the mismatch on the appropriate surface:
edit the introduction personally, or approve/correct the graph through its
existing controls.

### 11.3 Read-only writing coach

The coach launch prompt provides pointers—not copied prose bundles—to the
current introduction, complete `graph.json`, canonical `research.md`, and all
repositories in the project truth scope. The agent runs with read permission
for those paths and no write permission. No graph-side setting can widen it,
because there is no longer a graph-side setting to widen.

While the canonical state repo is unreachable, the introduction pointer may
target a read-only export of the unsynced local buffer and graph/`research.md`
pointers may target visibly stale local cache snapshots. Unreachable repository
locators are labeled unavailable; the app does not pretend the coach inspected
them. Provider readiness is still required for the call itself.

The system prompt states the authorship rule concretely:

- critique claims, structure, logic, literature coverage, and communication;
- quote existing human text only when diagnosing it;
- identify exact locations and prescribe editing actions;
- ask targeted questions that help the human supply the missing reasoning;
- never draft replacement sentences or paragraphs;
- never autocomplete, emit a paste-ready diff, or modify any file.

There is no Apply-suggestion control. The only path from advice into the paper
is the human typing in the editor.

### 11.4 Native session registry

**New chat** starts a native read-only Claude or Codex CLI session. **Resume**
uses that provider's native session id. The app neither copies the transcript
nor injects every historical paper chat into a new session. Every resume adds a
short instruction to reread the current pointers because the document and graph
may have changed.

There is no automatic daily, idle-time, or token-count restart policy. The user
chooses New chat when they want a clean native context; provider-native context
management applies inside a resumed session.

The coach panel reads the persisted result of the currently attached
`AgentTask`. It need not remain open while the provider runs, recreate the full
historical transcript before Resume, or own cancellation; the provider session
holds native context and the shared task surface owns lifecycle.

Prior Paper-coach sessions stay behind one compact, clickable history control.
Opening it permits selection or resume; while closed, the current conversation
remains the primary panel rather than sharing permanent space with the session
registry.

Chat and coaching surfaces do not teach through decorative copy. An empty
conversation is blank, and its composer has no sample prompt or placeholder.
The collapsed agent picker is one small box showing only the provider name
(`Codex` or `Claude`); model, reasoning, machine, permission explanations, and
locked/editable labels are absent from its resting state. Actual configuration
controls remain available on demand before the native session pins them.

The app data store records:

```python
@dataclass
class WritingSession:
    provider: Literal["claude", "codex"]
    native_session_id: str
    execution_machine: str
    project_id: str
    title: str | None
    model: str
    reasoning: str | None
    created_at: datetime
    last_resumed_at: datetime
    introduction_hash_examined: str
    graph_revision_examined: int
    research_md_hash_examined: str
```

Only sessions created from the Paper surface appear in its list. Node-chat
sessions remain attached to nodes; general app sessions remain attached to the
project. Discovered Claude/Codex conversations are refresh sources, not paper
sessions, unless a future explicit import feature is added.

Provider/model/reasoning settings are immutable for a native session. The New
chat dialog may override project defaults; switching later means creating a new
session. Missing CLI, authentication, model, read-only mode, or resume target is
shown as a launch error. There is no silent fallback.

Writing calls are never scheduled from refresh. The user starts or resumes them.

---

## 12. Stack and repo layout

**Backend:** Python, FastAPI, `uv` + `pyproject.toml`.
Everything hard lives here: spawning Claude Code/Codex (locally or remotely),
SSH transport, patch materialization, history manager, validator, conversation
indexer.

**Frontend:** Vite + React + Tailwind, served at `localhost`.
Agent results are read from the persisted background-task API; no view owns an
invocation through a request-bound stream.

**Entry point:** `rcp open [<project>]` (CLI launches the project index, registers
an optional supplied project, and opens the browser). When the data-directory
lock is already held by a healthy RCP server at the requested address, `open`
reuses it and registers the optional project through its API; it never attempts
to start a second server. If that owner is unavailable at the requested address,
`open` gracefully replaces it. `serve` is always an explicit ownership request:
it gracefully shuts down the current owner, waits for recoverable background
work to pause and the lock to release, and then starts the requested server. A
new server process builds the frontend before creating the app; `serve --reload`
additionally keeps both Python sources and the served frontend bundle under watch.

**Desktop app (Tauri wrapper) is v2.** Browser-first now means the frontend can
be wrapped later with little work.

Accepted cost: two toolchains (`uv` + `npm`) in one repo, plus a frontend build
step.

```
research-control-panel/
  pyproject.toml
  src/rcp/
    __main__.py           # CLI entry point
    projects.py           # persistent project registry + lazy project runtimes
    api/                  # FastAPI routes, SSE
    core/                 # graph, patches, materialize, validate  (pure)
    history/              # append-only log manager
    sources/              # claude/codex indexers, cwd matching, cursors
    agents/               # spawn primitive, prompt templates, provider launch
    paper/                # editor sync, coach policy, native session registry
    transport/            # local + ssh
    collectors/           # optional; mechanical dumps into .research/facts (D14)
  web/                    # Vite + React + Tailwind
  tests/
```

Per standing rules: public entry points, private helpers, and utilities stay in
separate modules; `core/` is pure and independently testable. The validator
lives in `core/` so admission and deterministic replay share one rule set rather
than duplicating validation logic (D12).

---

## 13. Build order

1. **Conversation indexer** — walk roots, read `cwd` from inside files, union
   across machines, match sessions to truth-repository aliases, index by
   `(truth_repository, machine, source, session_id)`, and filter raw inputs by
   run truth scope.
   Verifiable standalone against the real 46 MB corpus.
2. **Graph core** — dataclasses (including `Proposal`), slug ids, patch ops
   including guarded `set_project_truth_scope`, `materialize()`, validation
   rules including slug fuzzy-matching. Pure, unit-testable, no I/O.
3. **History manager** — append, materialize, slice, and atomically apply an
   approved project truth-scope change to the manifest.
4. **Agent primitive and background tasks** — construct the run-scoped raw
   locator projection, always include the global graph and `research.md`,
   expose selected repositories read-only in place, require exactly one
   scratch-folder `patch.json`, and apply it through core to the canonical state
   repo. Every agent invocation runs behind one durable `AgentTask` receipt
   outside the API event loop; chat and coaching results persist even when their
   panels close.
   **CLI-only at this stage** — prove the graph is good before building any UI.
5. **Seeding operation** — full-corpus read inside an explicit run truth scope,
   lineage bias, coverage recording, produces revision 1 with every node
   `asserted`. Run it for real against CRLP with `crlp-remote` selected and the
   remote checkout canonical; this is the first point where the design is
   falsifiable end to end.
6. **Comprehensibility layer** — glossary object, gated-card fields, validator
   banners, zero-context eval over stored proposals.
7. **UI** — persistent project index, no-write add-project wizard with five
   agent-role profiles, run truth-scope selector, guarded project-scope
   settings, Overview-first project shell, trust-view picker, Requires You,
   Research, DAG, Runs, glossary, and Paper projections, and detail
   drawer with visually explicit standing. A non-modal background activity bar
   and Activity inspector expose estimated progress, Pause, Resume, Retry,
   attempt lineage, effective run contract, and the persisted event trail. DAG
   interaction includes chained nested scrolling, brightness controls,
   fullscreen, and bulk or per-node pin release.
8. **Approval and review flow** — project-wide persistent local draft, visual
   Agree/Disagree with active-choice withdrawal, direct human prose edit,
   proposal and ambiguity decisions, stale-base refusal, and one transactional
   Sync that emits strict human patches and regenerates materializations once.
9. **Write-back** — canonical `research.md` generation and explicit injection
   into every app-launched agent, with no synchronized copies or repo-instruction
   edits.
10. **Node and project chat** — background task results, graph-patch-only writes,
    scope-appropriate transcripts under `.research/chat/`, and pinned effective
    agent settings on resume.
11. **Complete remote execution once local works** (D12). It ships in v1 but
    does not belong on the initial critical path, and is blocked on the
    outbound-network check (§4).
12. **Paper editor** — canonical `.research/paper/introduction.md`, template
    creation, embedded Markdown editing, local autosave/undo, remote atomic
    sync, base-hash conflict handling, and a user-resizable editor/coach split
    (D15, §11).
13. **Writing coach** — native Claude/Codex New/Resume registry, read-only
    launch sandbox, coaching prompt, pointer assembly, provider/model pinning,
    passive graph-change indicator, and compact on-demand session history
    (D16, §11).
14. **Optional, any time after step 5:** collectors (D14) — a cron'd `sacct` /
    `git log` dump into `.research/facts/`. Independent of everything above and
    a few lines each. Worth doing once a real refresh proves slow at gathering
    execution state, or once a perishable fact gets lost between refreshes;
    not before.

Steps 1–5 are the risky half and produce no UI. Resist reordering: a beautiful
UI over a graph that duplicates and mis-merges is worse than no tool, because it
launders bad state into apparent fact.

---

## 14. Verification plan

| What | How |
|---|---|
| Indexer and run scope | Run over the real corpus; assert every `ssh-*` session maps to the right project repository alias; select only `crlp-remote` and assert no raw locator or matched session from `crlp-laptop` enters assembled context |
| Global graph across run scopes | Build nodes from two project repositories, then refresh with only one selected → the complete graph and `research.md` remain in context while raw inputs from the other repo remain absent |
| Guarded project truth scope | Attempt membership change from a seed, refresh, or chat run → refused. Approve a stored `set_project_truth_scope` proposal → manifest changes atomically, state repo remains a member, and historical graph nodes remain |
| Canonical machine | Configure canonical state on GPU-A and any graph-agent profile or invocation on GPU-B → refuse it. Run Seed, Refresh, Node chat, and Project chat on GPU-A and read `.research/` in place. Place Paper coach independently and confirm it remains read-only |
| Canonical state | Stage review, wording, proposal, and ambiguity actions on the laptop → neither local nor remote canonical `.research/` changes. Press Sync → one state-workspace transaction advances the canonical log and regenerates materializations once. Interrupt a multi-patch local write before commit → replay sees none of it. Interrupt SSH publication before and after its batch commit point → remote history never exposes a prefix, repairable materializations never lead history, absent or uncertain commits cannot enter local replay, and a confirmed commit clears the draft while subsequent canonical work waits for automatic materialization repair. Repeat with a fully local project and verify the same draft/canonical boundary |
| Scratch-folder isolation | Codex: spawn a run with one selected repo → selected raw content is readable in place but writes into it fail; other project repos are not raw-accessible; global graph and `research.md` remain readable; the only successful write is `patch.json` inside the scratch folder. Claude: verify the same requested behavior and prompt contract, while recording that `acceptEdits` + `--add-dir` does not provide an OS-enforced repository-write denial |
| No copying | Spawn a run whose selected repository lives on another machine → the repository is read over SSH at its declared path, nothing is copied into a local cache, and no size-envelope refusal is possible. With the canonical state repo on the execution machine, `.research/` is read in place and only conversation slices are staged |
| **Identity idempotence** | Re-run refresh over an already-processed window → **zero new nodes, zero new edges, no `standing`/`status`/`validity` change**. Prose churn is expected and ignored. This is the single best smoke test in the system (D6) |
| Materialize | Deterministic — same log produces byte-identical `graph.json` across runs |
| Validation | Unit tests for each reject rule, especially "no gated transition in a seed/refresh/chat patch," "accepted-node edges require a proposal," "agent output cannot claim approval kind/author," "proposal approval ops must match verbatim," and strict standalone review/direct-edit shapes |
| Proposal staleness | Refuse a proposal with a future `base_rev` or omitted/extra derived dependencies. Create a valid proposal, mutate a related node, attempt approval → refused, proposal `withdrawn` |
| Staged review and edit | Stage Agree on an asserted node, edit its prose, then withdraw the active judgment → the drawer updates and survives restart while canonical files remain byte-identical. Sync once → strict approval patches append under one transaction, wording lands, standing is `asserted`, and `research.md` is regenerated only at that boundary |
| Recovery ladder (§6.4a) | Agent writes a valid patch under the wrong filename → found and applied silently. Agent writes a shape-invalid patch → the same session is asked to correct it and the run succeeds. Agent never writes a valid patch → the run fails after exactly two correction rounds rather than looping. Agent writes a well-formed patch that graph validation rejects → the run fails immediately with no correction round |
| Failure retains evidence | After a failed run, the scratch folder still exists and the patch text that failed validation is retrievable; after a successful run the scratch folder is gone |
| Coverage honesty | Hand-build a patch citing a `SourceRef` older than `coverage.earliest_timestamp` with no `set_coverage` → flagged. Then the same patch with `set_coverage` → clean, and the boundary moves |
| Scope expansion and late sessions | Select a project repo never used in a prior run, or add an approved repo/machine after revision 1; refresh → old sessions do not trigger an implicit full-history read; they appear as skipped coverage or as a deliberate boundary move (§6.6) |
| Derived cache lifecycle | Populate remote-log and session-slice caches beyond each TTL/count/byte bound, pin entries used by an active task, and run startup/index cleanup → expiration then deterministic LRU reaches every cap without touching sources or pins. Evict a used entry → it reconstructs from source; make the source unreachable → an explicit gap appears |
| Replay checkpoint | Materialize past revision 100, restart, and verify the newest prefix-hash-valid checkpoint plus suffix replay is byte-identical to full replay. Corrupt and remove the checkpoint in turn → both safely fall back to full replay |
| Refresh routing context | After human standing/edit/proposal decisions and a chat graph patch, assemble refresh context → `refresh_delta` starts after the last applied seed/refresh, contains current contested nodes, stays within 50 entries/16 KiB, and reports omitted count/revision range. A large session corpus yields only a bounded routing index with a pointer to inspect omitted sessions |
| Collector staleness | Cite a `.research/facts/` dump older than the previous refresh as an execution claim's only support → flagged (D14) |
| Entity resolution | Seed, then refresh with a session covering the same experiments; inspect for duplicates (acceptable) vs wrong merges (must be zero) |
| Card quality | Zero-context eval (§7) over sampled stored proposals |
| **Seed quality** | After the scoped full seed: (a) every node is `asserted` and `research.md` is empty; (b) spot-check three known abandoned threads — each is present as `superseded` lineage, not absent and not top-level noise; (c) the attention view is not swamped by historical items |
| End-to-end | Seed CRLP from real transcripts with the remote truth repo selected and canonical; ask: can the project be explained from the graph alone in under two minutes, without opening chat? |
| Accepted core delivery | `research.md` exists only in the canonical state repo, contains only accepted content, and is injected into an app-launched agent working in another project repo. An independently launched session is deliberately not tested or claimed |
| Trust-view picker | With accepted, asserted, and contested fixtures, verify Working/Accepted only/Review projections, contested nodes never drive headlines, the preset persists locally, and hidden unresolved counts remain visible |
| Research projection | Build a fixture containing connected question-to-hypothesis/decision-to-experiment-to-evidence paths plus orphaned records → Research renders the paths by stage and places the orphans only under Unconnected records, never in a flat all-node ledger |
| Runs projection | Combine agent tasks and experiment records with failure, pause, blocker, retry, healthy, and completed states → failed/paused/open-blocked roots appear first, retries nest under their logical roots, and the heading reports the observation-derived as-of time |
| DAG interaction | Select Research flow on a fixture whose stored relation arrows oppose semantic progression → nodes stay in question, hypothesis/decision, experiment, and evidence columns while arrows retain their real direction. Pinch over an off-center node → the graph zooms around that focal point while ordinary two-finger/wheel scrolling still pans and hands off to the project page at the matching boundary. Drag and inspect nodes before and after zoom without losing fullscreen, pins, filters, or graph truth. Drag nodes well beyond their auto-layout envelope without hitting the original bounds; compare repulsion extremes on one fixture and observe materially different spacing. Verify Brighten all, Dim all, native fullscreen entry/exit with node details open and visible, Release all pins, and per-node release from an active pin |
| Paper template and authorship | Create the paper → six headings appear once and can be freely changed. No agent can write `.research/paper/`; only the editor endpoint can change the canonical file |
| Paper non-authority | Edit the introduction to contradict an accepted hypothesis → graph, standing, and `research.md` remain unchanged; a refresh may create only an ambiguity or gated proposal |
| Offline paper sync | Edit while canonical state is unreachable → local draft survives with Unsynced status. Reconnect with unchanged base → atomic sync. Change canonical first → Conflict, with no automatic overwrite or merge |
| Coaching contract | Give the coach a paragraph with a known flaw → it identifies the location, asks questions, and prescribes an editing action without replacement prose, a Markdown diff, file writes, or an Apply path |
| Native writing sessions | Start and resume both provider types in read-only mode; only Paper-created sessions appear behind the compact history control; current pointers are reread; provider/model/reasoning stay pinned; unavailable model/resume target fails without fallback. Resize the editor/coach split and confirm no synthetic canonical-title/path banner precedes the authored Markdown |
| No automatic coaching | Refresh the graph past a session's recorded revision → passive stale-review indicator changes and no writing-agent process launches |
| Add-project wizard | Preflight a new local and remote project and assert no `.research/` path appears. Configure all five role profiles and assert readiness is checked on every selected machine. Confirm the local project → one manifest with fixed permission declarations is created and the workspace opens. Preflight an existing manifest → Connect is offered and its bytes remain unchanged |
| Post-setup settings | Change the default raw-input subset and all five surface profiles, save once, restart RCP, and assert the canonical manifest, remote bootstrap copy, launch dialogs, and fixed permission summaries agree |
| Agent launch surfaces | On Seed, Refresh, Node chat, Project chat, and Paper coach, expand and collapse the configuration box. In each chat surface, the collapsed control shows only `Codex` or `Claude`; the empty conversation and composer contain no instructional or sample text, while configuration and history remain on demand |
| Background lifecycle and retention | Start local and remote Seed/Refresh attempts, navigate freely, inspect phase/event/progress state, pause each without orphaning its provider process, and resume from an RCP-checkpointed native session plus its stable scratch folder. Caller-supplied and legacy untrusted session ids are refused. Restart RCP mid-run → normal shutdown persists `paused`; hard interruption persists `interrupted`; pause, interruption, and terminal failure retain scratch/output for 7 days, success removes them immediately, and linked Retry starts a fresh folder. Apply the task 100/90-day union with active retry lineage and writing-session 50/180-day union; no failed attempt materializes a partial patch, and only success reaches 100% |

The end-to-end check is the real one. Everything else is a proxy.

---

## 15. Implementation decisions and remaining questions

1. **Remote provider preflight — resolved.** Codex and Claude transcript roots,
   outbound reachability, CLI versions, authentication, and the login-shell PATH
   requirement were verified on the configured CRLP host.
2. **"New messages since refresh" counting** — needs to be cheap (no agent
   call). Probably a line-count delta from cursor per session, but sidechain and
   compaction records inflate it. Decide what counts as a "message" for this
   number, since it drives the human's decision to hit refresh.
3. **Multi-project UI and setup — resolved.** RCP starts at a persistent project
   index. Each card represents one manifest, one paper, and one global graph;
   clicking it opens the existing workspace. Add Project uses a separate
   no-write-preflight wizard for local/SSH repositories and creates or connects
   the manifest only after human confirmation. Repository membership within a
   project does not create repository-level graphs.
4. **Graph-agent concurrency — resolved.** A project-level agent-run lock
   refuses a second seed, refresh, node-chat, or project-chat invocation with a
   clear message.
5. **`git` interaction** — does the app commit `.research/` changes, or leave
   them for the human? Leaving them dirty in a research repo will be annoying;
   auto-committing surprises people. Recommend: do not commit; represent dirty
   state visually where repository status is already shown.
6. **Slug collisions** — two genuinely different experiments with the same
   natural slug. Suffix with a counter, and never reuse a retired slug.
7. **Seed cost envelope — resolved.** A full-corpus seed is a long unattended
   run (D13), so it uses the durable background lifecycle in D10. Pause or
   shutdown never materializes a half-seeded revision 1. Resume continues a
   complete native-session/scratch-folder checkpoint; Retry starts a clean
   linked attempt with a fresh folder.
   Progress remains labeled as an estimate rather than provider telemetry.
8. **Proposal supersession** — if refresh generates a new proposal covering the
   same transition as a pending one, does it withdraw the old one automatically
   or leave both? Recommend: withdraw the old, per duplication bias applying to
   *nodes*, not to the approval queue, which must stay short.
9. **Collector cadence and ownership — partly resolved.** Scheduling remains a
    deployment choice for the collector. Storage ownership is fixed: RCP ages
    out only explicit collector-owned timestamped dumps after 14 days and never
    deletes unknown or manually placed `.research/facts/` files (§6.7).
10. **Native writing-session preflight** — verify, on every supported execution
    machine, the exact Claude/Codex read-only launch flags, resume identifiers,
    authentication behavior, and configured lightweight model identifier. This
    is an environment check, not permission to add a silent fallback.

*Resolved through v0.3 grilling: one global graph across a guarded project truth
scope; per-run raw-input focus; canonical `research.md` delivered only to
RCP-launched agents; trust-view presets; and a human-only, non-authoritative
paper introduction with user-invoked native coaching sessions (D7–D8,
D15–D16, §9–§11).*

---

## 16. Explicitly out of scope for v1

- Collectors as **authoritative** sources — the graph trusting collector output
  directly rather than through agent prose. Optional collector *dumps* are in
  scope for v1 (D14); what is deferred is giving them standing of their own.
- Job monitoring and automatic refresh
- `Milestone` as a node type
- Active dispatch from the graph
- Patch-log compaction
- Desktop packaging
- Multi-user, sharing, collaboration
- Synchronized `research.md` copies or guaranteed context for independently
  launched agents
- OS-level prevention of an arbitrary independently launched same-user process
  editing the paper introduction
- Agent-written paper prose, autocomplete, Markdown patches, or Apply controls
- Automatic writing-coach calls after refresh
- Raw conversation ingestion or web/literature search in the writing coach
- Importing arbitrary external sessions into the Paper list, cross-provider
  session resume, or app-owned transcript compaction
- Full-manuscript, citation-manager, LaTeX-build, or submission workflow beyond
  the human-written introduction
- The event-sourced control plane from the source PDF's first half

---

## Appendix A — The one-line summary of the design

**The agent writes the graph and the human writes the paper; the graph is a
comprehension surface whose small accepted core is the only thing that flows
back to steer agent work.**

## Appendix B — Failure modes this design is built against

| Failure | Defense |
|---|---|
| Confidently wrong graph corrupts the mental model invisibly | `standing` axis, visually unmissable; only `accepted` steers |
| Approval queue recreates the bottleneck | Narrow gate set (D4); free writes |
| Agent jargon makes cards unreadable | Structural card fields + glossary + validator + zero-context eval (D5) |
| Wrong merges collapse distinct failures under one label | Duplication bias, non-destructive merges, gated merges (D6) |
| Refresh drifts / duplicates on re-read | Search-before-create instruction + slug fuzzy-match flag; identity idempotence is the tested property (D6) |
| Multiple repo paths produce competing graph histories | Exactly one canonical state repo owns `.research/`; all approved repositories contribute to one global graph (D8) |
| An agent changes project repository membership | `set_project_truth_scope` is proposal-backed and human-approved; no agent can edit the manifest (D4, D8) |
| A repository outside one run leaks raw inputs into it | Explicit `run_truth_scope`; a run omits other raw locators while still exposing the allowed global graph (D7, D12) |
| A refresh or chat agent edits project code | Selected repos are read in place and are not a write target; the scratch folder is the agent's only writable surface (D11, D12) |
| Coding agent corrupts the graph mid-task | `graph.json` is derived; hand-edits overwritten on materialize (D9) |
| Agent output tries to bypass the gate set | The sole scratch-folder patch enters through the shared validator; a rejected patch stays auditable in the append-only log and retained run artifacts but contributes nothing to materialized state (D12) |
| Gated change is rendered but not storable, so approval replays nothing | `Proposal` holds card + exact ops; approval replays verbatim (§5.3) |
| Seeded nodes can never enter the accepted core without human authority | Detail UI stages human Agree/Disagree judgment; direct prose edits clear the draft judgment to asserted; node chat supplies context without taking authority; only Sync commits the human draft (D2–D3, §6.3) |
| Noncanonical agents miss human-accepted direction | Every RCP launch injects canonical `research.md`; no unenforceable claim is made for independent sessions (§9) |
| A trust filter launders asserted content or hides contested work | Working/Accepted only/Review projections have explicit trust rules and hidden-item counts; contested nodes never headline (§10) |
| Approval applies a change written against a graph that has since moved | `base_rev` staleness check; stale proposals refused and withdrawn (§5.3) |
| Seeded graph silently omits project history nobody remembers omitting | Full run-scoped corpus by default; project repositories seen or never seen and any within-run history narrowing are recorded in coverage (D13) |
| Seeded history buries current state under dead ends | Lineage-not-news bias: superseded threads pre-collapsed (D13) |
| Graph knows an era it declares it has never seen | Patches citing pre-boundary history must call `set_coverage`; validator flags the ones that don't (D13, §6.4) |
| Scope expansion or a corrected path silently triggers a full-history read | Late-discovered sessions predating the boundary get tail cursors or a deliberate boundary move, never an implicit read (§6.6) |
| Stale collector dump reported as live job state | Dumps are timestamped and prompted as observations-at-a-time; claims resting on dumps older than the last refresh are flagged (D14) |
| Writing coach becomes a ghostwriter | Read-only CLI, coaching-only prompt, no replacement prose/diff/autocomplete, and no Apply path (D16, §11) |
| Human draft silently changes scientific truth | Introduction is non-authoritative; disagreement can only surface an ambiguity or gated proposal (D15, §11) |
| Offline draft overwrites newer canonical prose | Base-hash guarded sync; conflicts stop automation and require human reconciliation (D15, §11) |
| One endless writing chat bloats context or loses provider identity | Native provider sessions are listed and resumed individually; settings are pinned and New chat creates a clean session (D16, §11) |
| Refresh creates a new paper-attention queue | Only a passive stale-review indicator updates; writing calls are user-invoked (D16, §11) |
| Agent quietly reshapes the research question | Spine edits are diffable and non-destructive; prior text survives |
| Scope creep into an orchestrator before comprehension works | V1 agents change graph understanding only; human paper edits are explicit; v2 is where agent dispatch changes the project |
