# Open questions

Design questions that are **raised and evidenced but not decided**. This file is
deliberately not the blueprint: the blueprint records decisions, `docs/acceptance/`
records promises, and this file records what is still genuinely undecided.

An entry stays here until it is either decided — at which point the canonical
blueprint is updated in place and the entry is deleted here — or ruled out. Keep
the evidence with the entry, so the next person does not re-derive it.

---

## Q4 — Who authors glossary terms after the standalone Glossary surface is removed?

**Status:** open. Raised 2026-08-03. No decision.

Existing glossary entries remain useful as inline hover definitions, but the
researcher-facing glossary table and ontology editor are being removed. This
does not decide whether future terms are authored by graph agents, derived from
node prose, staged by a human correction, or omitted entirely.

Until that authority and lifecycle are decided, inline glossary support only
renders entries already present in canonical history. It adds no creation,
editing, or deletion path.

---

## Q1 — Should `RELATION_SPEC` widen to let evidence reach a blocker or a decision?

**Status:** open. Raised 2026-08-01. No decision.
**Governing section:** [Relations and graph structure](research-control-panel-blueprint.md#relations-and-graph-structure).

### The question

`supports` currently permits `Evidence → Hypothesis` only, as do `weakens`,
`refutes`, and `inconclusive`. Evidence therefore has exactly one legal target
in the whole vocabulary.

Two shapes have no legal encoding:

- `Evidence → Blocker` — an observation corroborating that an impediment is real.
- `Evidence → Decision` — an observation justifying a methodological choice.

Both are already named in v0.5 as the leading widening candidates, to be
recognized rather than rediscovered. The instruction there is to widen the table
if either recurs legitimately, and explicitly *not* to teach agents to work
around it.

### Evidence gathered so far

**One real instance exists, and it is deliberate.**
`examples/demo-project/state-repo` contains
`ev/external-path-match-study --supports--> blk/missing-optimizer-state`,
authored at patch revision 5 and carrying a permanent `relation-type-mismatch`
flag. The project never customized its ontology (`config_revisions: {}`), so the
edge was flagged the moment it was written — not ontology drift. Its own
`explanation` field says why it is there: *"This deliberately provisional edge
demonstrates a visible endpoint-type flag."* So it is both a demonstration of
flag rendering and a specimen of the second widening candidate.

**A layer-based objection was investigated and does not hold.** The concern was
that an epistemic relation reaching an action-layer node would break the
projection partition. It does not, for two reasons:

1. `layer` does not drive the Research/Runs split. `researchProjection.ts` and
   `runProjection.ts` never read it. Its only consumer is
   [`graphProjection.ts`](../web/src/graphProjection.ts), where it selects DAG
   edge highlighting — `meta` neutral, `seam` emphasized, matching layer
   emphasized, otherwise dimmed. It is a display attribute.
2. The table already crosses layers in four relations, and labels them
   inconsistently:

   | relation | crossing | declared layer |
   |---|---|---|
   | `tests` | experiment → hypothesis | `seam` |
   | `produces` | experiment → evidence | `seam` |
   | `has_decision` | research_question → decision | `action` |
   | `blocked_by` | research_question → blocker | `action` |

   `seam` exists precisely for cross-layer relations. Widening `supports` would
   produce more seam edges, not a new kind of violation.

**Most of the motivating path is already expressible.** For the flow
*evidence corroborates a blocker → blocker resolved into a decision → decision
governs an experiment → experiment yields evidence → evidence bears on a
hypothesis*, four of the six hops are legal today (`requires_decision`,
`governed_by`, `produces`, `supports`). Only the two candidate shapes are missing.

### What blocks a decision

Two sub-questions, neither settled:

1. **Widen `supports`, or add distinct relation names?** If `supports` may target
   a hypothesis, a decision, and a blocker, one name carries three meanings — *is
   evidence for the truth of*, *justifies the choice of*, *corroborates the
   existence of*. Separate names (`justifies`, `corroborates`) are clearer but
   grow a closed enum, and agents read relation names to interpret the graph.
2. **Is one instance enough?** v0.5 says widen when a shape recurs *repeatedly
   and legitimately*. The current count is one, in a fixture. That is an
   observation, not yet a pattern.

### Known mechanical consequence, whichever way it goes

`layer` is declared per relation **name**, not per edge —
[`models.py`](../src/rcp/core/models.py) stamps every edge with `spec.layer`. A
widened `supports` would stamp `epistemic` on edges that actually cross, so the
DAG would dim them when it should emphasize them.

The fix is to derive layer per edge from its endpoints: same layer at both ends →
that layer, different → `seam`. That makes `tests` and `produces` fall out
automatically instead of being hand-labeled, and corrects the existing
`has_decision` and `blocked_by` mislabelling. This is worth doing on its own
merits and is not contingent on the widening decision.

### Do not do in the meantime

Do not "fix" the demo fixture's flagged edge, and do not work around the table
with a project-custom relation. The flag is the observation the design asked for.

---

## Q2 — What belongs in Control v2 after completion-only watchers?

**Status:** open. Raised 2026-08-01. V1 boundary decided; v2 details are not.
**Governing section:** [Experiment control and watchers](research-control-panel-blueprint.md#experiment-control-and-watchers).

### Decided boundary

V1 watchers are generic, restart-durable completion checks. They do not stream
live output, interpret outcomes, own external work, or carry experiment-attempt
semantics. V1 adds no stale-watcher cleanup primitive and no hard repository
lease.

### Main Control v2 goal

Add wake-on-new-output while watched work is still running. Follow OpenClaude's
file-backed output plus durable-offset shape rather than holding logs in memory.
The unresolved contract is:

- how a watcher requests completion delivery, output delivery, or both;
- what constitutes a deliverable output delta;
- how repeated wakes are batched or debounced;
- when offsets advance relative to queued and delivered turns;
- how restart recovery avoids both dropped and repeated output;
- how a diagnostic turn may stop doomed external work without making the watcher
  itself the owner of that work.

### Direct graph manipulation (v2)

The human wants to drag between nodes to create an edge, choose its relation
type, and approve, refute, or delete an edge in place. This fits how the app
already treats human corrections as literal edits rather than agent requests.

Keep it separate from Experiment belief acceptance. Direct manipulation is UI
authority; belief acceptance is the settled Proposal path described in the
canonical blueprint. Building the first must not silently alter the second.

### Secondary v2 lifecycle questions

- When, if ever, are permanently degraded or abandoned watcher records cleaned
  up? The rows are cheap, survive app close, and need no v1 user-facing cleanup
  action.
- Does experiment control eventually need an enforceable repository lease, or is
  the v1 advisory active-loop marker enough? Human authority must remain explicit
  either way.

Graph-level scheduling across the research frontier is still separately deferred.
It is not part of this question merely because both features use the word
"control."

---

## Q5 — Should graph-writing agents be required to run an executable scanner?

**Status:** open. Raised 2026-08-03. No decision.
**Governing scenarios:** [S59](acceptance/S59-staged-graph-audit-skills.md) and
[S64](acceptance/S64-project-skill-workflow-selection.md).

Settings-owned package selection, immutable staging, compact context pointers,
and package receipts already ship under S64. What remains undecided is whether
RCP should add a `graph-scanner` package with an executable advisory check and
require a graph-writing agent to invoke it before finishing its initial Patch.

The proposed scanner would report structural quality problems such as likely
misattachments, duplicates, unexplained jargon, and unusually flat graph
regions. It would write only a bounded scratch report, never another graph
channel. Missing or unavailable execution would remain visible but would not
change the semantic validator's verdict or consume a correction round.

The decision is whether this prompt-enforced advisory step is worth its package,
receipt, protection, and execution machinery given that the uniform live Patch
validator already enforces graph correctness. S59 retains the proposed detailed
contract and acceptance drive; it does not authorize implementation until this
question is decided.
