# The kernel asks node-type questions

Confirmed by the human 2026-10-04. This record exists because naming a node
type at the point of use is the easy habit, and each new site makes the
research ontology harder to separate from everything else.

## What changed

Most of RCP does not depend on research: the Patch log, transitions and replay,
the Proposal queue, provider containment, conversations, bounded episodes,
watchers, compute jobs, artifacts, and team operation all work for any typed
graph. What is research-specific is the ontology and the meaning attached to
its types. A different kind of project, such as software development, would
keep that machinery and supply different answers.

Code across the backend and Web client named research types at the point of
use: `isinstance(node, Hypothesis)` checks, `{"research_question",
"hypothesis"}` literals, and the Evidence-to-Hypothesis relation set retyped in
six modules. Two pieces now separate that knowledge:

- **A project type.** `core/project_types.py` defines `ProjectType`, the
  answers one kind of project gives: which node types are protected beliefs,
  which are beliefs that outcomes bear on, which record outcomes, which a
  bounded loop drives, which hold a choice, which block, which frame
  questions; which relations connect an outcome to a belief, protect a
  belief's structure, or block; which field records retirement; and the type
  names prose uses. `core/research_type.py` holds the research answers.
  Kernel code calls `project_type_of(state)` and asks it.
- **A research layer.** A node type's own rules, which read fields only that
  type has (a Decision's ballot, a Hypothesis's status cause, an Experiment's
  attempt ledger), and research-only features (the Experiment loop, the
  research rendering) live in an explicit list of modules. Kernel code calls
  them; it does not restate them. The Web client has the same split, with
  `web/src/graph/researchType.ts` mirroring the backend answers and presentation.

Prose that describes enforcement, such as "an existing ResearchQuestion or
Hypothesis waits for a human", renders its type names from the project type,
so the agent contract cannot drift from the rule. Its output is byte-identical
to the hand-written text it replaces, so the authority policy digest is
unchanged. Methodology prose, such as how to write a falsifiable Hypothesis,
is research content and stays where it is.

`tests/test_project_types.py` and `web/tests/projectType.test.mjs` list the
research layer and count, for every other file, the research node types, node
classes, and research relations it still names. A count may fall, never rise.

## Why the project type is not persisted yet

Every project is a research project, so a stored project type would have one
possible value and would cost a persisted-format change, migration, and
transfer compatibility for nothing. When a second type exists it belongs in the
project's canonical identity, because replay must not depend on current
configuration. A project without that record is a research project, which
keeps replay deterministic for every existing project.

## What the counter still allows

Some research words outside the research layer are persisted or wire names,
not type knowledge: the Auto-research child kind `"experiment"`, notification
and digest kinds `"decision"` and `"blocker"`, episode-timeline actor kinds, an
artifact API key, and an unrelated `"evidence"` role in server operations.
Renaming them would cost a migration and decouple nothing. The single import of
the research type in `project_type_of` is the one place a second type would be
chosen.

## What the same pass removed

- **Auto-research seats.** The orchestrator could seat a worker only on an
  Experiment or Blocker, because those types had a "mechanically checkable
  exit". Nothing read the seat's state: worker completion, waiting, recovery,
  and quiescence come from route, task, and watcher rows, and the seat grants
  no graph authority. A worker may now sit on any existing node, and its
  instruction says what result ends the job.
- **Retiring Evidence.** Supersede and merge wrote `status="superseded"` on
  every node, but Evidence records its lifecycle in `validity`, so supersede
  and merge on Evidence failed while staging. They now set the field the
  project type names. No committed history changes, because every earlier such
  Patch was rejected.

## What a second project type would still need

This pass concentrates research knowledge; it does not make a second type
pluggable. A software project type (Goal, Requirement, Design, Change, Spike,
Verification, Decision, Blocker) would also need:

1. a registered node-model union in place of the closed one in
   `core/models.py`, whose typed JSON is hashed into persisted transitions;
2. the kernel's calls into the research layer routed through the project type
   rather than imported directly;
3. an agent Patch schema generated from the ontology instead of the
   hand-written per-type classes in `agents/schema.py`;
4. the Web client's ontology served by the API instead of mirrored; and
5. the project type recorded in canonical identity.
