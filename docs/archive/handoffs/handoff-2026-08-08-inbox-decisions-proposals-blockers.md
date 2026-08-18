# Handoff — the Inbox is Decisions, Proposals, and Blockers

> Archived 2026-08-17. Landed: the Inbox model and the section 3 rulings are
> implemented, carried by
> [S53](../../acceptance/S53-truthful-attention-and-run-surfaces.md) (rewritten in
> place, last passed 2026-08-08) and
> [S86](../../acceptance/S86-human-decides-a-decision.md) (last passed
> 2026-08-09). The protected-type rule that later widened the Proposal vocabulary
> arrived with [the authority core](handoff-2026-08-12-dispatch-authority-core.md).
> The "no code exists" state below does not hold. Retained only as historical
> design context; the canonical design lives in
> [`docs/research-control-panel-blueprint.md`](../../research-control-panel-blueprint.md).

**Date:** 2026-08-08
**State:** the model and every ruling in section 3 are **confirmed by the human**
in a design interview on 2026-08-08. No acceptance scenario has been rewritten
yet and no code exists. Write and confirm the scenarios in section 12 before
implementing.

**Relation to other work:** independent of, but touching, the
[permission design](../../design/identity-permissions-and-agent-profiles.md). This
handoff supplies its Decision-action split — see section 6.

**Updated 2026-08-09:** “Hypothesis-status-only” below is the implemented
ordinary-agent contract. The project-orchestrator design later adds protected
Proposals for any change to an existing ResearchQuestion or Hypothesis. It does
not restore Decision Proposals: the orchestrator decides Decisions directly.

Read [`AGENTS.md`](../../../AGENTS.md) first, then the blueprint's
[Human and agent authority](../../research-control-panel-blueprint.md#human-and-agent-authority)
section, then this file.

---

## 1. Why this exists

RCP has four representations for "a human needs to look at this," and one of
them duplicates another.

| Situation | Representation |
|---|---|
| A lasting research choice with options | **Decision** node |
| An agent recommends a specific graph change | **Proposal** |
| Something prevents progress | **Blocker** node |
| Uncertainty that requires no action | **Ambiguity** |

`Ambiguity` earns nothing. It never reaches `research.md`; it lives only in
`graph.json`, the attention counts, History, and the Inbox. Its two concrete
uses in the agent contract — missing hypothesis scope and missing ontology
vocabulary — are both cases where the agent has something to *say*, not
something to record.

Removing it exposed a second duplication that matters more: a **Decision
Proposal** and the Decision it targets are two Inbox entries demanding one human
act. That is the same fault one layer up.

The end state is three item types and three distinct human acts:

| Inbox item | The human act |
|---|---|
| Decision awaiting choice | **choose** an option |
| Pending Proposal (currently Hypothesis status; later protected orchestrator epistemic operations) | **approve or reject** a transition |
| Asserted open Blocker | **agree or contest** a claim |

This table is the complete **graph-attention** grammar; auto-research does not
widen it to completed Experiments, Evidence, decided Decisions, or resolved
Blockers. Team spaces separately add project-membership invitations to the
Inbox. An invitation is a non-graph account item whose action is **join or
decline**; it does not change which graph node types or statuses count as
research attention.

An auto-research campaign instead ends with a detailed HTML wrap-up available
from its campaign record. That report is where a returning human reviews the
Decisions and Blockers acted on, Experiments run, Evidence produced, and
epistemic Proposals still awaiting judgment. Its exact contract is owned by the
[orchestrator handoff](handoff-2026-08-07-orchestrator.md), not by this Inbox
predicate.

## 2. The one new concept: ripeness

`Decision.status` gains **`ready`**.

`open` is the resting state of every Decision — framed, not yet choosable. It is
**not** an attention item. `ready` is the agent's assertion that the choice can
now be made, and it is what puts a Decision in the Inbox.

Without this split the Inbox never empties, because `open` is where every
Decision is born and where most of them correctly sit for a long time. An inbox
that never empties stops being read, and then the Proposals and Blockers in it
stop being read too.

Full status set: `open | ready | decided | revisit | superseded`.

**Inbox membership for Decisions: `status ∈ {ready, revisit}`.** Single axis. No
`standing` clause — see ruling R4.

## 3. Rulings (all confirmed 2026-08-08)

| # | Ruling |
|---|---|
| R1 | Non-blocking uncertainty is **not** an attention item. No replacement channel is created for it as a graph object. |
| R2 | Existing projects are **drained by hand** before the removal lands. No migration code, no legacy Inbox section, no retained human close path. |
| R3 | `Decision.status` gains an explicit ripeness state `ready`. Inbox admits `ready` and `revisit`. |
| R4 | The human's "not yet" is `ready → open`, an ordinary human node edit. No `standing` filter for Decisions, no snooze state, nothing new recorded. |
| R5 | A patch that leaves a Decision in `ready` or `revisit` with fewer than 2 distinct `options` is **rejected at admission**. Plain `open` stays unconstrained. |
| R6 | **Ordinary-agent Decision Proposals are dropped.** The implemented ordinary-agent contract becomes Hypothesis-status-only. Creation is removed; approval, withdrawal, and replay stay. |
| R7 | Split the conflated Decision gate into two named permission actions, `decide_decision` and `queue_decision`, behind one stubbed predicate. |
| R8 | Ripeness criteria are **prompt guidance, never validated**. The prompt must direct the agent to inspect run-scope repositories and real experiment/code state, not only the graph. RCP never sets `ready` itself. |
| R9 | Inbox grammar: an item that **is a node** is a row that opens its node card; an item that is **not** a node (a Proposal) acts inline. No inline Decision ballot. |
| R10 | A Decision row shows **title plus a Ready/Revisit state chip**. Nothing else. |
| R11 | The snapshot count key is **`decisions_awaiting_choice`**, with one named predicate per side and a backend/frontend agreement test. |
| R12 | The ordinary-agent creation check narrows to `decide_decision` only: an ordinary-agent-created Decision may be `open` or `ready`, never carry `selected_option` or `status: decided`. `revisit` at creation is incoherent and refused. |
| R13 | [S53](../../acceptance/S53-truthful-attention-and-run-surfaces.md) is **rewritten in place**; one new `driver: pytest` scenario carries the agent contract. S86 is amended; the obsolete actor-ownership scenario is retired. |
| R14 | Ontology gaps and missing hypothesis scope are both **said in the answer**, not recorded as nodes. |
| R15 | The Seed/Refresh final answer is **persisted and displayed**, because it is currently discarded and R14 depends on it reaching the human. |

### Rulings that were considered and rejected

- **Fold Decision Proposals into the Decision card.** Rejected by R6. Folding is
  a renderer reconciling two records for one obligation; and a Decision Proposal
  requires a `governed_by` edge ([authority.py:64](../../../src/rcp/core/authority.py:64)),
  so the folded card would have been the rare path beside a bare ballot with no
  visible reason for the difference.
- **Extract the Decision ballot into a reusable component and render it in the
  Inbox.** Rejected by R9/R10. The ballot already exists in the node inspector
  at [DetailDrawer.tsx:380](../../../web/src/components/DetailDrawer.tsx:380) and is
  reused unchanged.
- **Use `standing` to carry ripeness.** Impossible: `Standing` defaults to
  `ASSERTED` ([models.py:15](../../../src/rcp/core/models.py:15)), so every new
  Decision would be born inside the Inbox. Ripeness must default to off.
- **Use `standing` to park a ripe Decision (the Blocker pattern).** Rejected by
  R4. For a Blocker, judging the agent's claim *is* the whole obligation. For a
  Decision it is not — choosing is — so exiting on standing would record that
  the human discharged an obligation they did not.
- **Enforce ripeness criteria at admission.** Rejected by R8: an agent that
  cannot pass the check will fabricate an Evidence node to satisfy it, which is
  worse than a premature `ready` costing one click.
- **A one-time conversion or archival flow for legacy ambiguities.** Rejected by
  R2 in favour of draining by hand.

## 4. Prerequisite — drain by hand (R2)

**Do this before any code lands.** As of 2026-08-08 the four registered projects
(all remote on `tianhaowang-gpu0.ucsd.edu`) held:

| project | open ambiguities | pending Proposals |
|---|---|---|
| continual-RL-plasticity | 0 | 0 |
| edit-agent | 0 | 0 |
| hypertree-or-whole-proof | 0 | 1 |
| vista-followup | **3** | 0 |

1. Open **vista-followup** and Resolve or Dismiss all 3 open ambiguities with
   the UI that still exists, then Sync.
2. Check whether hypertree-or-whole-proof's pending Proposal targets a Decision.
   If so, approve or reject it and Sync.
3. Re-count all four projects and confirm zero open ambiguities and zero pending
   Decision Proposals.

Re-run the count at implementation time; these numbers are a 2026-08-08
snapshot, not a fact about the design.

## 5. Legacy and replay — invariant 1 is not negotiable

`.research/patches/` is append-only and history is never rewritten. Everything
below is **admission-only**; `mode="replay"` behavior is unchanged, byte for
byte.

The seam already exists and needs no new machinery: `validate_patch` takes
`mode: Literal["admission", "replay"]`
([patch.py:30](../../../src/rcp/core/validation/patch.py:30),
[context.py:32](../../../src/rcp/core/validation/context.py:32)) and
[materialize.py:81](../../../src/rcp/core/materialize.py:81) passes `"replay"`.

**Retained forever, replay-only:**

- `Ambiguity` model ([models.py:378](../../../src/rcp/core/models.py:378)) and
  `GraphState.ambiguities` ([models.py:436](../../../src/rcp/core/models.py:436)).
- `create_ambiguities` / `resolve_ambiguities` materialization
  ([materialize.py:328](../../../src/rcp/core/materialize.py:328)) and state forking
  ([materialize.py:199](../../../src/rcp/core/materialize.py:199),
  [proposals.py:321](../../../src/rcp/core/validation/proposals.py:321)).
- Ambiguity history rendering in [delta.py](../../../src/rcp/history/delta.py)
  (lines 27–28, 117, 412, 508–515, 655, 713–719).
- `normalized_decision_proposal_ops`
  ([proposals.py:44](../../../src/rcp/core/validation/proposals.py:44)) and the
  legacy Decision-selection approval path in
  [approval.py:127](../../../src/rcp/core/validation/approval.py:127).

**Rejected at admission:**

- `create_ambiguities`, `resolve_ambiguities` — as legacy-only operations.
- Proposal creation targeting a Decision.

Legacy ambiguities never render and never count anywhere. After R2 there are
none open, so nothing is stranded in practice; a project restored from an old
backup shows a dormant record in `graph.json` and in History, which is correct.

## 6. Permission actions (R7)

The permission design's action vocabulary must not treat Decision status and
`selected_option` as one authority. That conflation blocks `ready`. **Split
it:**

| Action | Covers |
|---|---|
| `decide_decision` | write `selected_option`, or set `status: decided` |
| `queue_decision` | set `status` ∈ {`open`, `ready`, `revisit`} |

What the ordinary-agent boundary protects on a Decision is **the choice**, never
the queue position. `revisit` changes nothing about what was decided —
`selected_option` and its history stay intact — it only puts the question back
in front of the human. That is an assertion, which invariant 3 permits ordinary
agents to make.

**Interim implementation.** Actors do not exist yet. Name both actions now and
route every check through a single predicate — e.g. `permits(patch, action)` —
whose temporary body consults `patch.author`: `decide_decision` requires
`author == "human"`, `queue_decision` is always permitted. The future permission
service replaces that temporary rule in live admission rather than hunting
scattered `author == "agent"` conditionals. Replay remains independent of users
and profiles; it must not acquire a synthetic-actor lookup.

That future permission service deliberately gives the human-authorized project
orchestrator profile `decide_decision` as well as `queue_decision`. The ordinary
profile remains queue-only. The temporary `patch.author` rule cannot express
that distinction and is not the final orchestrator contract.

The permission module carries this split forward. Its sharper authority example
is `decide_decision`, which has the neighbouring `queue_decision` action that
must resolve differently. The obsolete actor-ownership scenario is not retained.

## 7. Shared contracts — land these first, serially

Per `AGENTS.md`, do **not** parallelize across these. One commit, then fan out.

**[`src/rcp/core/models.py`](../../../src/rcp/core/models.py)**
- `Decision.status` gains `"ready"`:
  `Literal["open", "ready", "decided", "revisit", "superseded"]`.
- `Ambiguity` and `GraphState.ambiguities` unchanged (replay).

**[`src/rcp/agents/schema.py`](../../../src/rcp/agents/schema.py)**
- Delete `AgentAmbiguity` ([:172](../../../src/rcp/agents/schema.py:172)),
  `CreateAmbiguitiesOperation`, `ResolveAmbiguitiesOperation` from the agent
  output union.
- `AgentDecision` status mirrors the new literal.
- Narrow the proposal field check at
  [:243](../../../src/rcp/agents/schema.py:243) from
  `fields <= DECISION_PROPOSAL_FIELDS or fields == HYPOTHESIS_PROPOSAL_FIELDS`
  to `fields == HYPOTHESIS_PROPOSAL_FIELDS`.
- These are strict-by-design schemas; this is a deliberate spec change.

**[`web/src/types.ts`](../../../web/src/types.ts)**
- Decision status union gains `"ready"`.
- `counts.open_ambiguities` → `counts.decisions_awaiting_choice`.
- Keep the `Ambiguity` type and `GraphState.ambiguities` — the wire still
  carries them for legacy records; nothing renders them.

## 8. Validation and authority

**[`src/rcp/core/authority.py`](../../../src/rcp/core/authority.py)**
- `DECISION_PROPOSAL_FIELDS` ([:10](../../../src/rcp/core/authority.py:10)) is
  dead once R6 lands — remove it and its import sites.
- `decision_is_experiment_input` ([:64](../../../src/rcp/core/authority.py:64)) is
  dead — the `governed_by` gate existed only for Decision Proposals.
- Add the `permits(patch, action)` predicate and the two action names (R7).

**[`src/rcp/core/validation/nodes.py`](../../../src/rcp/core/validation/nodes.py)**
- `requires_proposal` ([:184](../../../src/rcp/core/validation/nodes.py:184)):
  the Decision branch narrows to *changes touching `selected_option`, or setting
  `status: decided`*. It is no longer "any status change." Since Decision
  Proposals no longer exist, that condition means **refused**, not "gated pending
  approval" — the current ordinary-agent rejection must say *only a human may
  decide* rather than naming a Proposal path that is gone. The future
  orchestrator path resolves through its dedicated profile instead.
- `agent-created-decision-transition`
  ([:99](../../../src/rcp/core/validation/nodes.py:99)): narrow to `decide_decision`
  only. Allow `open` or `ready` at creation; refuse `selected_option` and
  `status: decided`. Rename the rejection code to name the action.
  *Rationale:* the old "born open" rule was trivially evadable — operations stage
  in written order (invariant 10b), so `create_nodes(open)` then
  `update_nodes(ready)` in one patch produced the identical result.
- New: reject any patch leaving a Decision in `ready` or `revisit` with fewer
  than 2 distinct `options` (R5), at admission only, for human and agent patches
  alike. A ballot with one choice is not a ballot.

**[`src/rcp/core/validation/proposals.py`](../../../src/rcp/core/validation/proposals.py)**
- `_validate_agent_proposal_boundary`
  ([:203](../../../src/rcp/core/validation/proposals.py:203)): delete the `Decision`
  branch. A Decision-targeting proposal is refused at admission with a message
  naming `decide_decision`.
- `decision_transition_error`
  ([:20](../../../src/rcp/core/validation/proposals.py:20)): keep — still used by
  the direct-choice path — and add that `revisit` requires a prior decision, so
  `revisit` at creation is incoherent (R12).

**[`src/rcp/core/validation/registry.py`](../../../src/rcp/core/validation/registry.py)**
- Mark `create_ambiguities` and `resolve_ambiguities`
  ([:81–84](../../../src/rcp/core/validation/registry.py:81)) legacy-only:
  rejected when `ctx.mode == "admission"`, unchanged at replay. Prefer a marker
  on `OpRule` over an ad-hoc conditional, so the vocabulary keeps declaring
  itself in one place.

**[`src/rcp/core/validation/approval.py`](../../../src/rcp/core/validation/approval.py)**
- Drop `"resolve_ambiguities"` from the single-op human allowlist
  ([:38](../../../src/rcp/core/validation/approval.py:38)).
- `_validate_direct_decision_choice` ([:201](../../../src/rcp/core/validation/approval.py:201))
  is otherwise unchanged. Confirm `ready` and `revisit` are both legal **source**
  states — nothing constrains the source today except the superseded refusal, so
  this should already hold, which is exactly why it gets an explicit assertion
  rather than an assumption.

**[`src/rcp/core/validation/ops.py`](../../../src/rcp/core/validation/ops.py)**
- Remove `depends_create_ambiguities` / `validate_resolve_ambiguities` /
  `depends_resolve_ambiguities` from the admission path (lines 480–514); keep
  whatever replay needs.

**[`src/rcp/control.py`](../../../src/rcp/control.py)**
- `decision_drift` ([:103](../../../src/rcp/control.py:103)): drop the
  `_has_pending_proposal` term ([:114](../../../src/rcp/control.py:114)). It is
  subsumed — an agent reopening a settled choice now sets `revisit`, and
  `decision.status != "decided"` already trips `moved`.

### The single sharpest current consequence

Until the project orchestrator profile lands, `selected_option` and
`status: decided` can only be written by a patch carrying
`human_action="decision_choice"`. No ordinary-agent or Proposal path exists.
The permission implementation will add the orchestrator as the second explicit
producer without reopening either of those paths.
Assert this directly in S94.

## 9. Service, snapshot, and counts (R11)

**[`src/rcp/service.py`](../../../src/rcp/service.py)**
- Delete `GraphSyncAmbiguityResolution` ([:220](../../../src/rcp/service.py:220)),
  `GraphSyncRequest.ambiguities` ([:233](../../../src/rcp/service.py:233)), its
  entry in the staged-id walk ([:243](../../../src/rcp/service.py:243)), the two
  call sites ([:945](../../../src/rcp/service.py:945),
  [:984](../../../src/rcp/service.py:984)), and the Sync patch construction
  ([:1182–1200](../../../src/rcp/service.py:1182)).
- `_snapshot` ([:619](../../../src/rcp/service.py:619)): replace the
  `open_ambiguities` filter with `decisions_awaiting_choice` — Decisions whose
  `status ∈ {ready, revisit}` — behind **one named module-level predicate**, not
  an inline comprehension.
- `counts` ([:652](../../../src/rcp/service.py:652)): `open_ambiguities` →
  `decisions_awaiting_choice`.

**[`src/rcp/projects.py`](../../../src/rcp/projects.py)**
- `attention_count` key tuple ([:433](../../../src/rcp/projects.py:433)) becomes
  `("pending_proposals", "decisions_awaiting_choice", "open_blockers")`.

**Naming note.** `counts.open_blockers` actually counts blockers that are open
**and** `standing == ASSERTED`; the name lost half its meaning and nothing forces
it back. **Do not rename it here** — it is wire churn across the snapshot, the
project store, and the web client for no behavior change. Raise it separately.
It is cited here only as the reason `decisions_awaiting_choice` must not be
called `open_decisions`: that name would be read as counting plain `open`
Decisions, which are precisely the ones excluded.

## 10. Web

**[`web/src/humanDraft.ts`](../../../web/src/humanDraft.ts)**
- Delete `AmbiguityDecision` ([:11](../../../web/src/humanDraft.ts:11)),
  `draft.ambiguities` ([:27](../../../web/src/humanDraft.ts:27)), the Sync payload
  field ([:43](../../../web/src/humanDraft.ts:43),
  [:375](../../../web/src/humanDraft.ts:375)), `stageAmbiguityDecision`
  ([:286](../../../web/src/humanDraft.ts:286)), and the count term
  ([:354](../../../web/src/humanDraft.ts:354)).
- **Loading an older persisted draft must not fail and must not retain staged
  ambiguity resolutions.** The shape guard at
  [:417](../../../web/src/humanDraft.ts:417) currently *requires*
  `parsed.ambiguities` to be a record — a stored draft written before this change
  must still load, with that key dropped. This is a real regression path and gets
  its own test.

**[`web/src/App.tsx`](../../../web/src/App.tsx)**
- Delete the `ambiguities` memo ([:1497](../../../web/src/App.tsx:1497)), the
  `onAmbiguity` wiring ([:2432–2437](../../../web/src/App.tsx:2432)), and the
  `stageAmbiguityDecision` import ([:104](../../../web/src/App.tsx:104)).
- Add `decisionsAwaitingChoice` as an **exported named predicate** beside
  `humanAttentionBlockers` ([:280](../../../web/src/App.tsx:280)) — not an inline
  `useMemo` filter — so the backend agreement test has something to bind to.
- `attentionCount` ([:2115](../../../web/src/App.tsx:2115)) becomes
  `pendingProposals + decisionsAwaitingChoice + openBlockers`.

**[`web/src/components/AttentionRail.tsx`](../../../web/src/components/AttentionRail.tsx)**
- Delete the ambiguity card, its Resolve/Dismiss actions, and the `Ambiguity`
  import.
- Add the Decision row: **title plus a Ready/Revisit state chip**, click opens
  the node card via the existing `onSelectNode`. Same shape as the Blocker row
  ([:208](../../../web/src/components/AttentionRail.tsx:208)) — no inline ballot, no
  options preview, no caption.
- Tile/heading labels become Pending proposals, Decisions awaiting choice,
  Blockers awaiting judgment.

**[`web/src/components/DetailDrawer.tsx`](../../../web/src/components/DetailDrawer.tsx)**
- The ballot at [:380](../../../web/src/components/DetailDrawer.tsx:380) is reused
  **unchanged** and is where every choice is made.
- Remove `pendingDecisionProposalCount` and its readout
  ([:52](../../../web/src/components/DetailDrawer.tsx:52),
  [:427](../../../web/src/components/DetailDrawer.tsx:427)) — Decision Proposals no
  longer exist.
- Confirm `decisionChoiceDisabled`
  ([:242](../../../web/src/components/DetailDrawer.tsx:242)) permits `ready` and
  `revisit`; it only excludes `superseded`, so it should already.
- The Decision status control must offer `ready → open` as the human's "not yet"
  (R4).

**[`web/src/views/ProjectOverview.tsx`](../../../web/src/views/ProjectOverview.tsx)**
- Replace the ambiguity term ([:21](../../../web/src/views/ProjectOverview.tsx:21),
  [:84](../../../web/src/views/ProjectOverview.tsx:84),
  [:86](../../../web/src/views/ProjectOverview.tsx:86)) with decisions awaiting
  choice.

Also sweep `web/src/views/GraphViews.tsx` and the ambiguity styles in
`web/src/styles.css`.

## 11. Agent-facing prose

**[`src/rcp/agents/prompts.py`](../../../src/rcp/agents/prompts.py)**

| Line | Change |
|---|---|
| [:47](../../../src/rcp/agents/prompts.py:47) | Ontology gap: **say it in the answer.** State plainly that a needed node or edge cannot be represented under the active ontology, name what is missing, and continue with what *can* be recorded. Do not create a node for it. |
| [:58](../../../src/rcp/agents/prompts.py:58) | Missing hypothesis scope: leave `scope` empty, say so in the answer. **Explicitly forbid** manufacturing a Blocker or a Decision for it — once Ambiguity is gone, Blocker is the only remaining door and the agent will reach for it unless told not to. |
| [:71](../../../src/rcp/agents/prompts.py:71) | Drop the `amb/` id prefix. |
| [:684](../../../src/rcp/agents/prompts.py:684) | Drop "ambiguities" from the deletion prohibition. |
| [:710](../../../src/rcp/agents/prompts.py:710) | Narrow Proposal rules to Hypothesis status transitions with an evidence cause. |
| new | The Decision lifecycle contract, stated **once**. |

The new ordinary-agent Decision block says: create a Decision `open`, or `ready`
when the choice is already makeable. Never write `selected_option` and never set
`status: decided` — this ordinary profile does not decide. Set `ready` when the
choice can actually be made, which in general means checking **the run-scope
repositories, the real state of the experiments, and the code** — not the graph
alone. As graph signals, `ready` normally implies no Blocker linked by
`blocked_by` is still
open, no Experiment linked by `governed_by` is pre-completion, and `rationale`
can say what the choice turns on. Use `revisit` to reopen a settled choice when
new evidence undermines it. Ripeness is never validated; a premature `ready`
costs the human one click.

While rewriting [:47](../../../src/rcp/agents/prompts.py:47): that line also directs
the human to change ontology "in Project Settings." **Verify that pointer** —
it is reportedly stale, and a known-false pointer in agent-facing prose is worse
than the instruction being replaced.

Same vocabulary sweep, no new rules, through:
- [`src/rcp/skills/graph-audit/SKILL.md`](../../../src/rcp/skills/graph-audit/SKILL.md)
- [`src/rcp/skills/workflows/research-graph-audit/WORKFLOW.md`](../../../src/rcp/skills/workflows/research-graph-audit/WORKFLOW.md)
- [`src/rcp/skills/evidence-triage/references/worked-examples.md`](../../../src/rcp/skills/evidence-triage/references/worked-examples.md)
- [`src/rcp/runs/shared.py:592`](../../../src/rcp/runs/shared.py:592) — the ambiguity
  op names in the semantic-op list.

## 12. Persist the Seed/Refresh answer (R15)

R14 routes both ambiguity cases into the agent's answer. For Seed and Refresh
that answer is **currently thrown away** —
[graph.py:799](../../../src/rcp/runs/graph.py:799) says so in a comment:

> An ingest run's deliverable is the patch file; its prose only confirms it was
> written, so the collected answers go unread.

`_ProviderOutcome.answers` ([shared.py:456](../../../src/rcp/runs/shared.py:456))
already collects the provider's labelled final assistant message and discards it
for ingest runs. Persist it on the run record and render it in the task
inspector beside the patch.

Invariant 11 already draws the answer/trace line — answers are the labelled
final assistant message, never the last text emitted. This change only stops
discarding the answer side for one surface. Without it, R14's instruction is
written but the words evaporate, which is worse than the Ambiguity it replaces.

## 13. Acceptance scenarios (R13)

**Write and confirm these before implementing.**

### Rewrite [S53](../../acceptance/S53-truthful-attention-and-run-surfaces.md) in place

`driver: browser`, status back to `pending`, `last_passed` cleared, re-driven in
a browser at the end. It currently asserts verbatim *"pending proposals + open
ambiguities + asserted open blockers"* and *"The Inbox tiles are Pending
proposals, Open ambiguities, and Blockers awaiting judgment"* — this change
rewrites those. A second scenario beside it would leave two `implemented`
promises contradicting each other about one surface.

New promise: attention is pending Proposals + Decisions awaiting choice +
asserted open Blockers. A Decision row is title plus a Ready/Revisit chip and
opens its node card; the ballot is the existing inspector one; a staged choice
stays visible until Sync exactly as a staged Blocker judgment does; `ready → open`
removes it; legacy ambiguities appear nowhere and count nowhere; the project
card, Inbox badge, Inbox heading, and tiles agree. Every assertion here lives in
the browser — draft survival across reload, row→card→ballot, four-surface count
agreement — so the browser is earned.

### New: `S94-decision-ripeness-and-the-agent-contract.md`

`driver: pytest`. Everything here is settled by backend calls and does not earn
a browser.

- `new_patches_cannot_create_or_resolve_ambiguities`
- `historical_ambiguity_patches_replay_identically`
- `new_patches_cannot_create_a_decision_targeting_proposal`
- `historical_decision_proposals_replay_and_remain_resolvable`
- `queue_decision_permits_open_ready_and_revisit_from_an_agent`
- `decide_decision_refuses_selected_option_or_decided_from_an_agent`
- `an_agent_created_decision_may_be_open_or_ready_but_never_decided`
- `revisit_at_creation_is_incoherent_and_refused`
- `ready_or_revisit_with_fewer_than_two_options_is_refused_at_admission`
- `plain_open_decisions_are_unconstrained`
- `a_direct_choice_is_legal_from_both_ready_and_revisit`
- `selected_option_and_decided_are_only_ever_written_by_a_decision_choice_patch`
- `decisions_awaiting_choice_matches_the_frontend_predicate`
- `a_seed_or_refresh_answer_is_persisted_and_readable`

### Amend

- **[S86](../../acceptance/S86-human-decides-a-decision.md)** — drop the
  agent-created-Decision-Proposal premise; its fixtures become legacy proposals
  from history. Keep the approval and withdrawal machinery and its tests: a
  project restored from a backup with a pending Decision Proposal must stay
  resolvable by hand, and deleting the withdrawal logic buys nothing.
- **S08, S21, S25, S51** — edit only where they name ambiguities.

## 14. Tests

**Replace, do not merely delete.** Add:

- Old persisted `HumanDraft` containing staged ambiguity resolutions loads
  successfully and retains none (the [:417](../../../web/src/humanDraft.ts:417)
  guard).
- Backend/frontend count agreement over one shared fixture graph carrying
  Decisions in every status and Blockers in every standing (R11).
- A Decision row opens the node card and the inspector ballot stages, survives
  reload, and Syncs.

**Touch:** `tests/test_prompts.py`, `test_agent_schema.py`,
`test_staged_graph_validation.py`, `test_demo_fixture.py`, `test_api.py`,
`test_history.py`, `test_proposal_boundary.py`, `test_sync.py`,
`tests/helpers.py`; `web/tests/humanDraft.test.mjs`,
`attentionRunsOntology.test.mjs`, `projectHistory.test.mjs`,
`decisionChoice.test.mjs`.

**The demo fixture** (`examples/demo-project/state-repo`) keeps its one
historical ambiguity as a **replay-compatibility fixture**. Do not edit its
materialized files. `test_demo_fixture.py` should assert it replays and does not
appear in any count.

**Migration warning from `AGENTS.md`.** Every test builds a fresh SQLite file, so
a green suite says nothing about an existing store. If section 12 adds a column
for the persisted answer, index it only in the migration block below the
`_ensure_column` calls, and verify by opening a copy of the real store.

## 15. Implementation order

1. **Drain by hand** (section 4). Blocking.
2. **Confirm the scenarios** (section 13). Blocking, per `AGENTS.md` step 0.
3. **Shared contracts, serial, one commit** (section 7): `models.py`,
   `agents/schema.py`, `web/src/types.ts`. Do not parallelize.
4. **Fan out**, one agent per boundary, issued in one block:
   - Graph core — sections 5 and 8 (`core/`, `control.py`)
   - Service/API — section 9 (`service.py`, `projects.py`)
   - Agent I/O — sections 11 and 12 (`agents/`, `runs/`, skills)
   - Web — section 10 (`web/src/`)
5. **Verify yourself**: `uv run pytest`, `uv run ruff check src tests`,
   `npm --prefix web run build`, `npm --prefix web test`, then
   `git add -A && uv run pre-commit run --all-files`.
6. **Drive the rewritten S53 in a browser.** `preview_start` with `rcp`; exercise
   the Inbox, the Decision row, the inspector ballot, staging, reload, Sync, and
   `ready → open`; check `read_console_messages`, `read_network_requests`, and
   `preview_logs`. S53's driver is `browser` and green unit tests do not
   discharge it.
7. **End-of-session sweep** of `pending` / `blocked-external` scenarios.

## 16. Not decided here

- Whether `counts.open_blockers` should be renamed to match its actual filter
  (section 9). Raised, deliberately out of scope.
- Whether the Project Settings ontology pointer at
  [prompts.py:47](../../../src/rcp/agents/prompts.py:47) is stale (section 11).
  Verify while rewriting the line.
- Whether an enforced cold-readability contract belongs on `ready` Decisions —
  non-empty `question` and `consequences`, the way `incomplete-gated-card` flags
  Proposals. Proposed and withdrawn during the interview as not load-bearing;
  revisit only if bare ballots turn out to be unreadable in practice.
