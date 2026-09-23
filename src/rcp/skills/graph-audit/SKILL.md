---
id: graph-audit
kind: skill
label: Graph audit
version: 3.4.0
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

**Untestable claims.** Flag a Hypothesis that no observation could show false, including one that
only answers its ResearchQuestion yes or no, or whose predictions restate the statement. Flag an
Experiment that `tests` a Hypothesis without `expected_outcomes` written before its results, and a
design that measures a stand-in for the claim's quantity without listing it in `proxies`.

**Broken structures.** For each node, read what it is trying to be and check that its connections
deliver it: a Hypothesis serves a question, Evidence from an Experiment bears back on what that
Experiment tested, a Decision belongs to a question or governs an Experiment, and a Blocker stops
something. Check each structure in [common structures](references/structures.md) that the graph
uses, and report its listed failures. Treat RCP's connection warnings in `graph.json`'s
`validation_messages` as leads, not verdicts.

**Relations hiding their reasoning.** Require an explanation of why each relation holds.
- An Experiment `tests` a Hypothesis it can discriminate and `produces` the Evidence it generated.
- `governed_by` and `blocked_by` mark genuine input gates only. Check complete action chains rather
  than treating every Experiment without a Hypothesis or Decision as an orphan.
- Each assessment is calibrated for its own Hypothesis; the same Evidence may bear differently on
  another. Flag an assessment on a relation that does not carry one.

**Proxies read as the real quantity.** Flag a claim stated about what a proxy stands for when its
Evidence measured only the proxy and says nothing of how well the proxy holds. Flag Experiment
`limitations` that the Evidence qualifications or `research.md` silently drop. Flag a `produces`
`expectation` that the observation and the Experiment's `expected_outcomes` do not bear out, and a
`diverged` result read as neither a protocol defect nor a finding.

**Missing truthful roles.** Flag Evidence with no provenance, a missing known producing Experiment,
or a methodological `result` or `diagnostic` role that conflicts with its observation; a
Blocker that blocks nothing; or an Experiment with no stated test or role in an action plan. A planned
precursor can name its intended downstream gate in its design until an observation exists; do not
require future Evidence or a `produces` edge. Accept honest isolation such as a newly recorded observation awaiting placement.

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

Separate observation from recommendation. Identify who may make each suggested change from the
current task contract. The report grants no authority to perform its suggestions.

## Boundaries

Do not edit canonical `.research` files during an audit. Do not invent causal history or explanations
for missing relations. If the vocabulary cannot represent an observed relation, report the exact gap
without silently choosing new ontology.
