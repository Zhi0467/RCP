# Open questions

Design questions that are **raised and evidenced but not decided**. This file is
deliberately not the blueprint: the blueprint records decisions, `docs/acceptance/`
records promises, and this file records what is still genuinely undecided.

An entry stays here until it is either decided — at which point it moves into a
blueprint amendment and is deleted here — or ruled out. Keep the evidence with
the entry, so the next person does not re-derive it.

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
**Governing section:** [v0.5 §`RELATION_SPEC` — typing and layers](archive/research-control-panel-blueprint-v0.5.md).
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

---

## Q2 — What belongs in Control v2 after completion-only watchers?

**Status:** open. Raised 2026-08-01. V1 boundary decided; v2 details are not.
**Governing section:** [v0.7 D29](blueprint-v0.7.md).

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

Keep it separate from [Q3](#q3--what-exactly-does-the-human-accept-when-an-experiment-produces-evidence).
Direct manipulation is UI authority; Q3 is about what unit a human accepts when
a loop reports a result. Building the first must not silently answer the second.

### Secondary v2 lifecycle questions

- When, if ever, are permanently degraded or abandoned watcher records cleaned
  up? The rows are cheap, survive app close, and need no v1 user-facing cleanup
  action.
- Does experiment control eventually need an enforceable repository lease, or is
  the v1 advisory active-loop marker enough? Human authority must remain explicit
  either way.

### Releasing stuck work — decided shape, not yet built

An attempt whose watcher can no longer answer leaves the experiment permanently
un-runnable: a nonterminal attempt marks the loop active, and `attempts` is not
human-editable. V1 needs a human release, and the shape is settled:

- **RCP never decides that a watcher is dead.** `degraded` is already mechanical
  — the last check exited neither 0 nor 1. The node reports the fact and its age
  ("last answered 3 days ago", plus the last error) and the human reads it. No
  threshold, no inference; the same reason "unknown" was deleted from the check
  contract.
- **One action per object, scoped by what it is attached to.** A watcher armed by
  an experiment attempt releases through **Stop attempt**, which closes that
  attempt as `cancelled` and drops its watchers in one human `approval` patch. A
  watcher armed by an ordinary Work turn has no attempt, and releases through
  **Stop watching**. Two labels, never two buttons on the same object.
- **Not an agent path.** Cancelling asserts the external run is finished, which
  needs knowledge of the machine RCP cannot see — and a broken watcher never
  wakes an agent to be asked in the first place.
- **Display is a timeline, not a dashboard.** Attempt rows with their pinned
  decisions and start/finish, watcher rows with last-answered and last error.
  Counts appear only inside a gate reason, where they explain a refusal.

Graph-level scheduling across the research frontier is still separately deferred.
It is not part of this question merely because both features use the word
"control."

---

## Q3 — What exactly does the human accept when an experiment produces evidence?

**Status:** **decided 2026-08-01; implemented by S50.** The human settled it: the
unit of acceptance is the belief change, not the edge. See "Decision" below;
the rest of this entry is kept as the reasoning that led there.
**Governing sections:** [v0.7 D25-D26](blueprint-v0.7.md) and
[S41](acceptance/S41-bounded-experiment-control.md).

### Decision

What a human accepts is **the belief change**, carrying its evidence edge as the
cause. Not the edge on its own, and not a second standing model.

- The evidence node and the epistemic edge are **asserted** by the loop, as they
  are today. They are facts about a run that happened; the loop is entitled to
  record them.
- The **hypothesis status change** is the Inbox item — one proposal, one gated
  card, one judgment. Approving it applies the status change and records the
  `BeliefTransition` with the edge as its cause.
- `Edge` gains no `standing` field. A seeded graph does not acquire hundreds of
  unaccepted edges.

This resolves the tension noted in resolution 2 below: the loop does *not*
propose the edge, so nothing about assertion changes. What widens is only the
loop's proposal scope, by exactly one narrowly checkable shape — target must be a
hypothesis the bound experiment `tests`, and the ops must be that hypothesis's
status plus a belief cause naming an edge created in the same patch. Proposing a
status change does not violate the anchor; the anchor is that the loop may never
*apply* one.

S50 implements this as the second and only other agent Proposal shape. The shared
model-facing policy in `src/rcp/core/authority.py` fixes the cause to the evidence
edge and keeps that wording aligned with agent admission. S41 may remain pending
for its broader browser and operational-control drive; the graph authority
contract itself is covered hermetically.

### The mismatch

V0.7 says a successful loop asserts an evidence edge, creates exactly one Inbox
item, and that human acceptance makes the edge accepted. The current graph has no
such mechanical state:

- standing belongs to nodes; `Edge` has no standing field;
- Inbox contains pending proposals, open ambiguities, and open blockers, not
  asserted edges;
- accepting a node in its detail drawer is a human action, but it is not an
  Inbox item and does not itself update a downstream belief.

The v0.7 validator can therefore admit an asserted Evidence node and epistemic
edge while preserving the rule that the loop cannot change a hypothesis status,
but it cannot honestly satisfy S41's final acceptance step.

### Plausible resolutions

1. Make the human accept the Evidence node, then separately edit the downstream
   belief. This uses existing standing but is neither one Inbox item nor one
   atomic judgment.
2. Put the evidence, edge, and belief transition inside one Proposal. This uses
   the existing Inbox and atomic approval path, but the loop would propose the
   edge rather than assert it, and D26's proposal scope would need to expand
   beyond upstream decisions.
3. Add standing and review semantics to edges. This matches the v0.7 wording but
   creates a second standing model and is the largest change.

Resolution 2 was chosen with one correction: the evidence and edge remain direct
assertions, while only the belief transition is inside the Proposal. The cause
is the same-patch evidence edge, so one Inbox judgment moves the belief without
inventing edge standing.
