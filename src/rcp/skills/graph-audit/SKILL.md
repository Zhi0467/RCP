---
id: graph-audit
kind: skill
label: Graph audit
version: 3.0.0
description: Audit a research graph when asked for a read-only structural review of claims, relations, node identity, lifecycle consistency, or rendered summaries; report defects without editing canonical state.
dependencies:
---

# Graph audit

Audit what the graph tells a reader, not only whether its JSON is valid. Produce a report; do not
repair canonical state unless a separate outer task explicitly asks for a later graph change.

## Read in order

1. Read `research.md`. List the claims and action state a first-time reader would believe.
2. Read `graph.json`. Trace each item to its nodes, relations, standing, evidence, and lifecycle.
3. Compare node titles within each type to find split or duplicate identities.

## Check

**Claims outrunning support.** Flag a conclusion that no Evidence establishes, a supported
Hypothesis carried only by qualified or unrelated Evidence, or prose that drops a recorded caveat.

**Relations hiding their reasoning.** Require an explanation of why a relation holds. Check that
an Experiment `tests` a Hypothesis it can discriminate, `produces` the Evidence it generated, and
uses `governed_by` or `blocked_by` only for genuine input gates. Check complete action chains rather
than treating every Experiment without a Hypothesis or Decision as an orphan.

**Missing truthful roles.** Flag Evidence with no provenance or producing Experiment when one is
known; a Blocker that blocks nothing; or an Experiment whose role is expressed neither through
`tests`, `produces`, nor an action-gate chain. Accept honest isolation such as a newly recorded
observation awaiting placement.

**Split identity.** Flag duplicate nodes that divide one entity's claims, evidence, or action
relations. Prefer reusing an existing identity over adding a near-copy.

**Lifecycle drift.** Flag a Decision, Blocker, or Experiment whose status conflicts with later
nodes or evidence. Evidence may inform a Decision through `informs` or bear on a Blocker through
`addresses` without choosing the Decision or changing the Blocker's status; report the mismatch
instead of inferring the transition.

**Stale ambiguities.** Flag an Ambiguity that later graph content answered or made irrelevant.

## Report

```markdown
## Observations
What the graph currently says, with node ids and supporting paths.

## Concerns
For each concern: the defect, involved nodes, evidence, and likely reader error. Order by impact.

## Suggested actions
Name the smallest correction and who has authority to make it.

## Checked and sound
Briefly list the important paths verified as coherent.
```

Separate observation from recommendation. Label standing, approval, and truth-membership changes
as human-owned. Do not describe every lifecycle status correction as human-only; identify authority
from the surrounding task contract and graph rules.

## Boundaries

Do not edit canonical `.research` files during an audit. Do not invent causal history or explanations
for missing relations. If the vocabulary cannot represent an observed relation, report the exact gap
without silently choosing new ontology.
