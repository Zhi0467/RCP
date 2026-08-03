# Handoff — UI fixes, cuts, and graph-audit skills

Written 2026-08-03 after a UI-level review session driving the running app
against the `how-to-predict-plasticity-loss` project. Every defect below was
reproduced through the browser and then traced to a file; every file:line
reference was read, not recalled.

Read [`AGENTS.md`](../AGENTS.md) first. The default working mode applies:
acceptance scenario before code for anything user-visible, implementation fanned
out to subagents along module boundaries, verification and diff review kept by
the main agent.

---

## Decisions already settled

1. **Inbox blocker tile — decided.** Drop the `scientific`/`design` filter and
   relabel the tile "Open blockers", alongside the A1 nav-badge fix, so every
   counter in the app agrees. Folded into A1.
2. **Per-chat config cut (B2) — decided.** Remove the expandable pickers from
   `NodeChat` and `PaperWorkspace`. Leave `RunDialog` (deliberate Seed/Refresh
   launch) and `ProjectSettings` (where the profile now lives) intact.
3. **Prose diff (C2) — decided: deterministic.** See C2 for the method; the
   short version is that the diff already exists and the work is to stop
   rendering ids in it, not to generate new prose.

---

## Batch A — independent, parallelizable

No shared contracts. Four separate agents can run these concurrently.

### A1. Nav badge counts all open blockers

**Defect.** Five surfaces report "needs attention" and two of them use a
different definition of blocker, so the same screen shows 13 in its header and
9 in the nav badge.

| Surface | Now | Formula |
|---|---|---|
| Project card | 13 | `pending + ambiguities + open_blockers` — [projects.py:418](../src/rcp/projects.py) |
| Inbox header | 13 | `proposals + ambiguities + blockers` — [GraphViews.tsx:893](../web/src/views/GraphViews.tsx) |
| Inbox 3rd tile | 0 | blockers filtered to `scientific`/`design` — [GraphViews.tsx:900](../web/src/views/GraphViews.tsx) |
| Nav badge | 9 | uses the same filtered set — [App.tsx:1700](../web/src/App.tsx) |
| Overview / DAG chip | 4 | all open blockers |

The fixture project has four blockers, all `implementation`/`infrastructure`.
`blocker_type` values are enumerated at
[models.py:154](../src/rcp/core/models.py).

**Change, two places.**

1. `attentionCount` at [App.tsx:1700](../web/src/App.tsx) uses all open blockers
   rather than `scientificBlockers`.
2. The Inbox tile at [GraphViews.tsx:898-904](../web/src/views/GraphViews.tsx)
   drops its `scientific`/`design` filter and is relabelled **"Open blockers"**.

`scientificBlockers` ([App.tsx:1136](../web/src/App.tsx)) then has no consumer —
delete it rather than leaving it dead. `blocker_type` stays on the model; only
these two attention counters stop discriminating on it.

**Done when.** On the fixture project the project card, Inbox header, and nav
badge all read 13; the Inbox tiles read 5 / 4 / 4 and sum to the header; the
Overview and DAG blocker chips still read 4.

### A2. Failed task cards stop showing live progress

**Defect.** A task that has already failed keeps rendering its progress bar —
observed as "Estimated progress 3% · about 5m left · 9s elapsed" on a task whose
headline was **Failed**.

**Where.** [`AgentTaskInspector.tsx`](../web/src/components/AgentTaskInspector.tsx)
and [`AgentTaskActivity.tsx`](../web/src/components/AgentTaskActivity.tsx). Find
where the progress block renders and gate it on a non-terminal status. Terminal
statuses are visible in
[runProjection.ts:15-16](../web/src/runProjection.ts) — `failed`, `paused`,
`interrupted`, `succeeded`.

**Done when.** The Agent tasks drawer shows no progress bar or ETA on a failed,
succeeded, or interrupted attempt. Elapsed/finished time may stay.

### A3. Runs view stops surfacing failed chat tasks

**Defect.** The research-facing "Runs & experiments" view headlines a raw Python
traceback (`PermissionError: [Errno 13] …`) and internal repair language ("The
repair left patch.json byte-identical to the rejected patch") because failed
node/project chat tasks land in its **Needs action** section. Those belong in
the Agent tasks drawer, which already shows them properly.

**Where.** [GraphViews.tsx:702-760](../web/src/views/GraphViews.tsx)
(`ExecutionView`) calls `buildRunTaskProjection` at
[runProjection.ts:19](../web/src/runProjection.ts) with every task.

**Change.** Filter chat-surface tasks out of what the Runs view projects.
`AgentTaskKind = AgentSurface` ([types.ts:14](../web/src/types.ts)), so the kinds
to exclude are `node_chat`, `project_chat`, `paper_coach`. Keep `seed` and
`refresh` — those are ingestion runs and belong in Runs.

Do the filtering at the `ExecutionView` call site, not inside
`buildRunTaskProjection`. That helper is policy-neutral and is also what the
Agent tasks drawer relies on; giving it a `kind` parameter would push policy
into shared plumbing, which invariant 10 forbids.

**Also update `AGENTS.md`.** The human-preferences entry currently reads "Runs
mixes agent tasks and experiments". Narrow it to ingestion runs and experiments,
and say chat tasks live in the Agent tasks drawer.

**Done when.** With the fixture project's four failed chat tasks present, Runs
shows only blockers and experiments under Needs action, and all four remain
visible and inspectable in the Agent tasks drawer.

### A4. Cut the ontology editor

**Rationale.** A researcher does not design a schema. The six shipped node types
are the product.

**Where.** [`OntologyEditor.tsx`](../web/src/components/OntologyEditor.tsx),
mounted at [ProjectSettings.tsx:523](../web/src/views/ProjectSettings.tsx),
imported at line 20.

**Scope carefully.** Remove the *editing surface* only. Custom types, fields,
and relations already present in a project's ontology must still materialize,
validate, and render — existing projects may have them. This is a UI removal,
not an ontology-model change. Do not touch
[`src/rcp/core/models.py`](../src/rcp/core/models.py) or the ontology parts of
[`src/rcp/config.py`](../src/rcp/config.py).

Check whether any test asserts the editor renders; update rather than delete
coverage of the underlying ontology.

**Done when.** Settings no longer offers custom type/field/relation editing, the
fixture project still loads and renders every node, and `uv run pytest` is green.

### A5. Paper editor renders Markdown

**Defect.** The Paper editor is a bare `<textarea className="markdown-editor">`
at [PaperWorkspace.tsx:357](../web/src/views/PaperWorkspace.tsx). There is no
renderer and no preview toggle — the author sees literal `#` and `##`. The
`paper-sheet-preview` block at line 285 is decorative placeholder text on the
empty state, not a renderer.

**The renderer already exists.** `react-markdown` and `remark-gfm` are
dependencies ([web/package.json](../web/package.json)) and are wired up in
[`chatMarkdown.ts`](../web/src/chatMarkdown.ts) for chat replies. Reuse that
configuration so paper and chat render identically; do not introduce a second
Markdown pipeline.

**Recommended shape.** A Write/Preview toggle over the same pane, not a third
column. The editor/coach split is already human-resizable and adding a permanent
third pane would squeeze both. Persist the toggle per project alongside the
other UI prefs.

**Constraint.** Preserve the existing dirty/sync/save behaviour and the
canonical-conflict resolution controls at
[PaperWorkspace.tsx:340-356](../web/src/views/PaperWorkspace.tsx). Preview is a
read-only view of unsaved editor content.

**Done when.** Typing `## Methods` in the editor and switching to Preview shows a
rendered heading; the word count, SYNCED badge, and save path are unchanged.

### A6. Node detail drawer is resizable

**Gap.** The conversation list has a resize separator ("Resize conversation
list") and the paper split resizes, but the node drawer does not.
[`DraggableWindow.tsx`](../web/src/components/DraggableWindow.tsx) handles
position and viewport clamping only — lines 27-47 — with no resize handle, and
the docked drawer is fixed-width.

**Change.** Add drag-to-resize to both drawer states: a width handle on the
docked drawer's inner edge, and corner/edge resize on the undocked
`DraggableWindow`. Persist the size the same way the conversation list persists
its width. Keep the existing clamp so a resized window cannot be dragged off
screen.

**Done when.** The drawer can be widened and narrowed by dragging, the size
survives closing and reopening the drawer, and the undocked window still clamps
to the viewport on window resize.

**Second, smaller bug in the same area.** The node drawer stayed open when I
switched from Research to Chats and covered the conversation. Either dismiss it
on view change or make it view-scoped.

---

## Batch B — backend contract, land serially before its consumers

### B1. Provider change must not inherit the previous provider's model

**Defect, reproduced end to end.** Switching a chat's provider from Codex to
Claude launched `claude --model gpt-5.6-luna`, which the Claude CLI rejected:
*"There's an issue with the selected model (gpt-5.6-luna)."*

**The frontend is correct** — this was verified, not assumed.
`providerChange` at [providers.ts:65](../web/src/providers.ts) returns
`model: ""`, and after switching, both the `<select>` and the persisted
localStorage config held `""`.

**The loss is on the wire and in the resolver.** `null` is overloaded:

1. The client sends `model: config.model || null`
   ([NodeChat.tsx:302](../web/src/components/NodeChat.tsx)) — cleared `""`
   becomes `null`, meaning *"use the provider default"*.
2. The resolver at [service.py:1321-1339](../src/rcp/service.py) starts from the
   stored manifest profile and overrides only non-`None` fields, so `null` means
   *"leave the stored value alone"*:
   ```python
   base = self.manifest.agent_profile(surface)
   if provider is not None: updates["provider"] = provider
   if model is not None:    updates["model"] = model
   ```
3. The manifest's `project_chat` profile is `provider: codex, model:
   gpt-5.6-luna` (confirmed via `GET /api/projects/{id}`). Provider is
   overridden; model survives.

**Fix (the human chose this option).** In the resolver, a provider change
invalidates the model unless the caller supplied one explicitly:

```python
if provider is not None:
    updates["provider"] = provider
    if provider != base.provider and model is None:
        updates["model"] = ""
```

Chosen over "make the client send `''`" because it holds for *any* caller that
changes provider without clearing the model, including future ones. `""` is
already a valid stored value — `seed` and `refresh` profiles use it.

**Check every caller** of this resolver before landing: `_pinned_to_profile`
([shared.py:299](../src/rcp/runs/shared.py)) and its users in
[`runs/work.py`](../src/rcp/runs/work.py) and [`runs/coach.py`](../src/rcp/runs/coach.py),
plus the Settings save path. Settings saving an explicit provider+model pair
must keep working.

**Test.** `tests/` — resolve a surface whose stored profile is
`codex/gpt-5.6-luna`, pass `provider="claude"` with `model=None`, assert the
result's model is empty. Add the converse: passing an explicit model with a
provider change keeps that model.

**Done when.** The unit test passes and, in the served app, switching a chat's
provider and sending a turn launches on the new provider's default model.

### B2. Cut per-chat agent configuration pickers

Depends on B1 landing first — B1 is what makes the Settings-owned profile
trustworthy once the per-chat override is gone.

**Rationale.** Provider, model, reasoning, and machine are set once. The
expandable per-chat panel is also the surface where B1's bug was reachable.

**Where.** [`AgentConfigControls`](../web/src/components/AgentConfigControls.tsx)
is used by `NodeChat` (line 444), `ProjectSettings` (463), `RunDialog` (109),
and `PaperWorkspace` (456).

**Scope (confirm first — see Decisions).** Remove the expandable pickers from
`NodeChat` and `PaperWorkspace`. Leave `RunDialog` and `ProjectSettings`.

**Preserve two things.** `AGENTS.md` requires that in chat and coaching the
resting state is one tiny provider-name box — keep that box, now non-expandable.
And keep the **Raw truth inputs** repository-scope control
([NodeChat.tsx:454-463](../web/src/components/NodeChat.tsx)); it is per-turn
context scope, not agent configuration.

**Done when.** A chat shows the provider name only, with no way to change
provider/model/reasoning/machine from the chat; the repository scope control
still works; Settings still configures all four.

---

## Batch C — smaller features

### C1. Glossary becomes inline hover definitions

**Rationale.** The Glossary panel is empty at revision 5 on a project dense with
private jargon (MOPD, theta0, fst_text_floor, GEPA-lite, WARP-lite, A7,
CISPO/GSPO, Mundlak). A nav slot that is always empty trains the reader to skip
it. Definitions are useful exactly where the term appears.

**Change.** Remove `GlossaryView`
([GraphViews.tsx:856](../web/src/views/GraphViews.tsx)) and its nav entry.
Add best-effort inline matching: where a glossary term appears in rendered node
prose, chat replies, or proposal cards, show its plain definition on hover.

**Constraints.** Best effort and non-destructive — never alter the underlying
text, never let a missed or spurious match change meaning. Match whole terms
only. Keep it cheap: this runs over every rendered node body, so precompute the
term index once per graph revision rather than scanning per render.

**Note the open question.** Who authors glossary terms is undecided — the human
raised it and it is not settled here. Add an entry to
[`docs/open-questions.md`](open-questions.md) rather than deciding it inside
this implementation. This item makes existing terms visible; it does not create
a writing path.

**Done when.** With at least one glossary term present, hovering that term in a
node body shows its plain definition, and the Glossary nav entry is gone.

### C2. Plain-language revision diff

**Want.** "What changed between r4 and r5" in ordinary language on the Overview
— no node ids, no operation names, no counts-of-things.

Not this: *"5 nodes updated, 1 decision status changed, 3 edges added."*
This: *"We fixed the third stream order to code-first. That added update 70 to
the probe grid, which added two checkpoint saves per stream."*

**The diff already exists.** Every patch carries `summary` and `change_summary`
([models.py:434](../src/rcp/core/models.py)), and `GET /api/projects/{id}/history`
already returns both per revision. Revision 5 of the fixture project returns
five perfectly good sentences. The problem is not that prose is missing — it is
that **both producers write node ids into it**:

- Agent side: `change_summary` is in the patch schema
  ([schema.py:302](../src/rcp/agents/schema.py)) but is essentially ungoverned —
  the only prompt mention is an incidental clause at
  [prompts.py:213](../src/rcp/agents/prompts.py).
- Human side: `service.py` builds strings like
  `f"{node_id} is now {request.standing}."` in a dozen places (lines 819-1280).

So this item is *four small changes*, not a generator — and the leverage is
lopsided. **Item 1 is where the quality comes from; items 2-4 are the renderer
and the safety net.** Do not let the renderer absorb the effort that belongs in
the prompt.

1. **Teach the producers to write plain prose (primary).** Add real
   `change_summary` guidance to the patch prompt contract: one sentence per
   change, ordinary language, name things the way a reader would say them, no
   ids, no operation names, no counts. The field is already being written on
   every patch — governing it costs nothing at runtime and improves every
   revision from then on. Change the `service.py` human-action strings to use
   titles for the same reason.
2. **Id → title resolution pass (safety net).** Node ids are structurally
   recognizable by prefix (`hyp/`, `dec/`, `prop/`, `ev/`, `exp/`) and
   resolvable against the materialized graph at that revision. Substitute the
   title when rendering. This catches a producer that regresses, and it fixes
   every patch already in the log — which prompt work cannot reach.
3. **Op-driven fallback** for any patch whose `change_summary` is empty. Group
   ops by kind and render counts with titles — "Recorded a decision: *Third
   stream order permutation*." Derivable entirely from ops plus state.
4. **Quote consequences, do not infer them.** The causal half of the target
   sentence — *"that added update 70 to the probe grid, which added two
   checkpoint saves per stream"* — is **not** derivable from patch operations;
   it is inference across nodes. But it already exists verbatim in the
   proposal's `if_accepted` prose. Quote that field rather than generating it.

**Known limit, state it rather than working around it.** Deterministic
generation gives you what changed, which things by name, and the consequence the
graph already states. It does not give you novel cross-node causal narrative. If
that is wanted later it needs an agent — but do not add one here.

**Where.** [`src/rcp/history/delta.py`](../src/rcp/history/delta.py) already
builds `RefreshDelta` / `RefreshDeltaEntry` from the patch log
(`build_refresh_delta`, line 62), with entry builders for standing transitions,
recent changes, and chat entries, plus size-bounding helpers. Extend that rather
than starting a new module. Surface on the Overview and in the History drawer.

**Constraint.** The diff is derived from the append-only patch log and is an
output — never authored or hand-edited (invariants 1 and 2).

**Done when.** Opening the fixture project at revision 5 shows a readable
sentence or two describing what r4→r5 changed, containing no node ids, and a
patch with an empty `change_summary` still renders something truthful.

---

## Batch D — design first, do not start with code

### D1. RCP-shipped graph-audit skills

This is the largest item and the one most likely to be built wrong if started
from the code. **Write the acceptance scenario and get it confirmed before
planning the implementation** (`AGENTS.md` step 0).

**The problem it solves.** Observed in the fixture project: the research question
"Comparing candidate mechanism predictors" is completely empty, while the three
mechanism-predictor hypotheses (entropy collapse, functional drift, weight-space
drift) sit under a different question. Another question's Ideas column is a flat
pile of fourteen Decisions. Forty of 41 nodes are `asserted`. The graph is
syntactically valid and semantically disorganized — the agent writes nodes it
can validate but not nodes a first-time reader can navigate.

**The intent.** Ship a set of official RCP skills alongside the app, with
context assembly handing agents pointers to them, so an agent can audit the
graph it just wrote. Candidate skills the human named:

- scan the graph for structural problems (orphan questions, misattached nodes)
- judge whether node prose is comprehensible to a first-time reader
- identify nodes that should be merged
- identify nodes that should be dropped

### Settled design

These were decided by the human; treat them as given, not as options.

- **A skill is a folder**, following ordinary skill structure — a `SKILL.md`
  carrying the instruction, plus any deterministic scripts in the same folder.
  This dissolves the tool-vs-instruction question: the folder is the unit, and
  whether a skill executes something is an implementation detail inside it that
  its own `SKILL.md` describes.
- **Staged per run, never copied into `.research`.** Skills are for
  RCP-launched agents only and must **not** be discoverable by an ordinary agent
  session someone opens in the project repo. That rules out `.research/skills/`
  and takes the canonical-state write, the append lock, and the version-drift
  problem off the table with it.
- **Versioned.** Each skill folder carries its own version, independent of the
  RCP release, and every run records which skill versions it staged.
- **Compact pointers in the contract.** The prompt carries id, a one-line
  when-to-use, and the staged path per skill — never a skill body. The agent
  reads the folder when it decides the skill applies.

### Delivery — follow the validator client, but fix how it ships

The Work validator client is the precedent: it is staged with `_stage_task_input`
into the local and remote stage, and the resulting path is baked into a command
handed to the agent in its contract
([work.py:252-266](../src/rcp/runs/work.py)).

Reuse that shape, but **not** its packaging. The validator client's source is a
string literal — `VALIDATOR_CLIENT_SOURCE` at
[patch_validator.py:55](../src/rcp/runs/patch_validator.py), 97 lines. That is
fine where it is and needs no retrofit: it is a single copy of pure mailbox
plumbing (read `patch.json`, write a request file, poll for a response, exit
with a code) containing no validation logic, so the drift failure recorded in
`AGENTS.md` "Repeated failures" — two copies of the same logic diverging — has
nothing to diverge from.

Skills are different in the way that matters. They are *many* folders, growing
over time, each potentially containing scripts. Embed those as string literals
and you recreate exactly the conditions that entry describes: multiple copies,
none locally testable, none linted or typechecked, drifting as they multiply.
So skills ship as real files on disk under `src/rcp/skills/<skill-id>/` and are
staged as files. Do not start them as embedded strings.

**The transport already supports this.** `RemoteRunStage.put_directory(source,
label)` exists at [run_stage.py:153](../src/rcp/transport/run_stage.py). Stage
the skill folder with it rather than inventing per-file staging.

**Register in one module.** Following the [`providers.py`](../src/rcp/providers.py)
pattern — id, label, version, one-line when-to-use, folder path, and which
surfaces receive it, all in one place, imperative parts included. Do not scatter
skill definitions across the modules that invoke them.

### First skill: the graph scanner

The human's chosen first skill. The agent runs it **after writing `patch.json`
and before finishing**, to see what it should change in the patch.

**Position it against the existing validator, and keep them distinct.** Both
are patch-time checks on a staged client, so state the difference explicitly in
the scenario or they will merge into each other:

| | Validator ([patch_validator.py](../src/rcp/runs/patch_validator.py)) | Graph scanner |
|---|---|---|
| Question | Will RCP accept this patch? | Should it have been written this way? |
| Nature | Mandatory, blocking, RCP-owned semantics | Advisory, agent-invoked |
| Output | valid / invalid / unavailable, exit codes matter | A report the agent reads and acts on |

Share the staging and request/response plumbing. Do **not** fold the scanner's
judgment into the validator's verdict — a badly attached but legal patch must
still be applyable, or advisory quality checks become a hard gate nobody
intended.

**What it should catch** — grounded in what the fixture project actually shows:
a research question with no attached nodes while hypotheses that answer it hang
off a different question; fourteen sibling Decisions under one question with no
grouping; nodes whose prose only makes sense to someone who already knows the
project.

### Invariants this must respect

- **Invariant 3.** Agents assert or propose; humans hold authority. "Drop this
  node" and "merge these two" are *proposals*, never assertions. Node removal
  already has a scenario in flight —
  [`docs/acceptance/S52-explicit-rejection-and-node-removal.md`](acceptance/S52-explicit-rejection-and-node-removal.md)
  — read it first and stay consistent with it.
- **Invariant 4b.** One way to get a patch out of an agent. A skill's output is
  a report the agent reads, or operations in the same `patch.json`. Never a
  second graph-change channel.
- **Invariant 10.** Skills belong to specific run policies. No shared helper may
  take a `kind`/`surface` parameter to decide which skills to stage — that
  decision is policy and belongs in the caller.
- **Permission contracts** are fixed by capability
  ([config.py](../src/rcp/config.py) `permissions_for()`). A skill cannot widen
  what its surface may do. Decide explicitly which patch-writing surfaces get
  the scanner: Work, and/or Seed/Refresh, whose permissions differ.

**Invariants this must respect.**

- **Invariant 3.** Agents assert or propose; humans hold authority. "This node
  should be dropped" and "these two should be merged" are *proposals*, never
  assertions. Node removal already has a scenario in flight —
  [`docs/acceptance/S52-explicit-rejection-and-node-removal.md`](acceptance/S52-explicit-rejection-and-node-removal.md)
  — read it first and stay consistent with it.
- **Invariant 4b.** One way to get a patch out of an agent. A skill's output is
  either a report the agent reads, or operations in the same `patch.json`. Do
  not add a second graph-change channel.
- **Invariant 10.** Skills belong to specific run policies. Decide which
  surfaces get which skills; do not add a shared helper that branches on surface.
- **Permission contracts** are fixed by capability
  ([config.py](../src/rcp/config.py) `permissions_for()`). A skill cannot widen
  what its surface may do.

### Questions the scenario must still answer

Delivery is settled; behaviour is not.

- Is running the scanner the agent's choice, or a required step in the Work
  patch contract before it may finish?
- Is its report visible to the human at all, or purely an agent-internal step?
  If visible — Runs, the task inspector, or the Work receipt?
- Does acting on the report cost an extra correction round against the existing
  bounded `work_patch_correction` budget?
- What happens when the scanner's advice contradicts a human `accepted`
  standing? The human's judgment wins, but the scanner needs to know that.
- Which surfaces stage it — Work only, or Seed/Refresh too?
- How is a skill version surfaced when reconstructing what a past run did?

**Do not** begin by adding skill files. Begin by writing the scenario in
[`docs/acceptance/`](acceptance/README.md), including the UI path, and
confirming it.

---

## Verification

Per-batch checks are named above. Before reporting any batch complete:

```bash
uv run pytest
```

```bash
uv run ruff check src tests
```

```bash
npm --prefix web run build && npm --prefix web test
```

```bash
uv run pre-commit run --all-files
```

A formatter pass that modified files is not a successful final check — stage the
changes and rerun until clean.

**UI verification is required** for A1, A2, A3, A5, A6, B2, C1, and C2 — every
one is a change whose failure mode is visible only in the browser. Serve the app
and drive it:

```bash
uv run rcp serve --host 127.0.0.1 --port 8421
```

The human usually already holds the lock on 8421. Probe
`http://127.0.0.1:8421/api/health` and reuse a healthy owner; never kill their
process. Check `read_console_messages` and `read_network_requests` alongside the
server log — a route returning 500 while the page still renders is a silent
failure.

**End-of-session sweep.** This work touches views covered by existing scenarios.
Before finishing, run the staleness check and re-drive anything this work made
runnable or made wrong:

```bash
grep -l "^status: \(pending\|blocked-external\)" docs/acceptance/S*.md
```

## AGENTS.md updates this work requires

- **Human preferences** — narrow the Runs projection entry (A3): ingestion runs
  and experiments, with chat tasks in the Agent tasks drawer.
- **Human preferences** — record that agent configuration is set once in
  Settings and chat/coaching surfaces display the provider name only (B2).
- **Human preferences** — record that the Paper editor renders Markdown (A5) and
  that the node drawer is resizable like the conversation list and paper split
  (A6).
- **Repeated failures** — add B1 as a one-liner: *a `null` model on the wire
  meant "provider default" to the client and "keep the stored value" to the
  resolver, so switching provider launched the previous provider's model.*
- **Fan-out table** — add `src/rcp/skills/` when D1 creates it: owns the skill
  registry, skill folders, and their staging contract.
- **Human preferences** — record the settled skill design (C2/D1): skills are
  folders staged per run for RCP-launched agents only, never copied into
  `.research`, never discoverable by an ordinary agent session in the project
  repo; each is independently versioned and the contract carries pointers, not
  bodies.
- **Human preferences** — record that patch prose quality is governed primarily
  by the prompt contract, with deterministic rendering as the safety net rather
  than the source of quality.
