# The kernel asks node-type questions

Confirmed by the human 2026-10-04. This record exists because naming a node
type at the point of use is the easy habit, and each new site makes the
research ontology harder to separate from everything else.

## What changed

Most of RCP does not depend on research: the Patch log, transitions and replay,
the Proposal queue, provider containment, conversations, bounded episodes,
watchers, compute jobs, artifacts, and team operation all work for any typed
graph. What is research-specific is the ontology and the meaning attached to
its types. A different domain, such as software development, would keep that
machinery and supply different answers.

Code outside the ontology needed those answers but spelled them out in place,
as `isinstance(node, Hypothesis)` checks or `{"research_question",
"hypothesis"}` literals. The set of relations by which Evidence bears on a
Hypothesis was retyped in six modules. `core/roles.py` now holds the answers,
and kernel code asks it:

- which existing node types are protected beliefs (invariant 3b);
- which relations connect an outcome to a belief;
- which field records that supersede or merge retired a node.

`tests/test_node_type_roles.py` counts the node-type names and node classes each
backend module still names, against a committed baseline. A count may fall,
never rise; a fall must be locked in by regenerating the baseline. `core/models.py`
defines the types and `core/roles.py` answers questions about them, so neither
is counted.

## What the same pass removed

- **Auto-research seats.** The orchestrator could seat a worker only on an
  Experiment or Blocker, because those types had a "mechanically checkable
  exit". Nothing read the seat's state: worker completion, waiting, recovery,
  and quiescence come from route, task, and watcher rows, and the seat grants
  no graph authority. The rule was a type name standing in for a claim no code
  made. A worker may now sit on any existing node, and its instruction says
  what result ends the job.
- **Retiring Evidence.** Supersede and merge wrote `status="superseded"` on
  every node, but Evidence records its lifecycle in `validity`. Agents could
  create, edit, and remove Evidence, but supersede and merge on Evidence failed
  while staging. They now set the lifecycle field `core/roles.py` names. No
  committed history changes, because every earlier such Patch was rejected.

## Stress test, not a second product

The goal is a codebase in which a second ontology would be cheap, not to build
one now. Mapping a software-development ontology onto RCP shows what the
kernel would need to ask:

| Question the kernel asks | Research | Software sketch |
| --- | --- | --- |
| What scopes the work? | ResearchQuestion | Goal |
| Which existing beliefs are protected? | ResearchQuestion, Hypothesis | Goal, Requirement, Design |
| What does a run test? | Hypothesis | Design |
| Which node does a bounded loop drive? | Experiment | Change, Spike |
| What gates a run? | decided Decisions, open Blockers | the same, plus prerequisite Changes |
| What does a run produce? | Evidence | Verification |
| Who chooses? | Decision | Decision (an ADR) |

The largest remaining couplings are, in order:

1. The closed node union in `core/models.py`, whose typed JSON is hashed into
   persisted transitions.
2. The Experiment control contract across `control.py`,
   `core/validation/experiment_loop.py`, and `runs/experiment_loop.py`.
3. Decision and belief-status rules.
4. The hand-written per-type agent schema in `agents/schema.py`.
5. The base ontology hardcoded in the Web client.

Each one moves into `core/roles.py`, or into a later ontology registry, when
work next touches it. None needs moving in advance.

## Limits of the rule

The rule does not rename persisted strings such as `experiment_loop`, table
names, or routes. A rename would cost a migration and decouple nothing.
Research prose in prompts and skills is ontology content; it moves with the
ontology when one is split out, not site by site. The counter covers only the
Python backend; the Web client's copy of the base ontology is tracked here
until it is served by the API.
