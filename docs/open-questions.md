# Open questions

Design questions that are **raised and evidenced but not decided**. This file is
deliberately not the blueprint: the blueprint records decisions, `docs/acceptance/`
records promises, and this file records what is still genuinely undecided.

An entry stays here until it is either decided — at which point it moves into a
blueprint amendment and is deleted here — or ruled out. Keep the evidence with
the entry, so the next person does not re-derive it.

---

## Q1 — Should `RELATION_SPEC` widen to let evidence reach a blocker or a decision?

**Status:** open. Raised 2026-08-01. No decision.
**Governing section:** [v0.5 §`RELATION_SPEC` — typing and layers](research-control-panel-blueprint-v0.5.md).
v0.6 does not touch it.

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
