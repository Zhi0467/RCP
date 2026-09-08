---
id: graph-audit
kind: skill
label: Graph audit
version: 3.1.0
description: Audit a research graph when asked for a read-only structural review of claims, relations, node identity, lifecycle consistency, or rendered summaries; report defects without editing canonical state.
dependencies:
---

# Graph audit

Audit what the graph tells a reader, not only whether its JSON is valid. Produce a report without
repairing the graph or writing a Patch. Any requested repair is a later step under the current
task's graph authority.

## Read in order

1. Read `research.md`. List the claims and action state a first-time reader would believe.
2. Read `graph.json`. Trace each item to its nodes, relations, standing, evidence, and lifecycle.
3. Compare node titles within each type to find split or duplicate identities.

## Check

**Claims outrunning support.** Flag a conclusion that no Evidence establishes, a supported
Hypothesis whose claim exceeds its Evidence's scope or qualifications, an applicable current
Evidence-to-Hypothesis edge missing its claim-relative assessment, or prose that drops the edge's
scope or qualifications. Treat a historical unassessed edge as legacy uncertainty, never as an
implicit weight.

**Relations hiding their reasoning.** Require an explanation of why a relation holds. Check that
an Experiment `tests` a Hypothesis it can discriminate, `produces` the Evidence it generated, and
uses `governed_by` or `blocked_by` only for genuine input gates. Check complete action chains rather
than treating every Experiment without a Hypothesis or Decision as an orphan. On each
Evidence-to-Hypothesis epistemic edge, verify that the relation states direction and the assessment
separately states relevance, weight, optional scope, and qualifications. The same Evidence may bear
differently on different Hypotheses. Flag an assessment attached to Hypothesis-to-Hypothesis
`contradicts`, `produces`, `informs`, `addresses`, or another non-applicable relation.

**Missing truthful roles.** Flag Evidence with no provenance, a missing known producing Experiment,
or a methodological `result` or `diagnostic` role that conflicts with its observation; a
Blocker that blocks nothing; or an Experiment with no stated test or role in an action plan. A planned
precursor can name its intended downstream gate in its design until an observation exists; do not
require future Evidence or a `produces` edge. Never interpret a legacy global strength label as a
current edge weight. Accept honest isolation such as a newly recorded observation awaiting placement.

**Split identity.** Flag duplicate nodes that divide one entity's claims, evidence, or action
relations. Prefer reusing an existing identity over adding a near-copy.

**Lifecycle drift.** Flag a Decision, Blocker, or Experiment whose status conflicts with later
nodes or evidence. Evidence may inform a Decision through `informs` or bear on a Blocker through
`addresses` without choosing the Decision or changing the Blocker's status; report the mismatch
instead of inferring the transition.

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

Separate observation from recommendation. Existing ResearchQuestion and Hypothesis changes require
Proposals; approval remains human-owned. Identify authority for Decision choice, standing, and
lifecycle updates from the current task contract and graph rules. The report grants no authority
to perform its suggestions.

## Boundaries

Do not edit canonical `.research` files during an audit. Do not invent causal history or explanations
for missing relations. If the vocabulary cannot represent an observed relation, report the exact gap
without silently choosing new ontology.
