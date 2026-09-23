---
id: evidence-triage
kind: skill
label: Evidence triage
version: 3.2.0
description: Triage Evidence before creating or materially updating it, or audit load bearing Evidence for provenance, methodological role, validity, claim-relative assessments, and action handoffs to Decisions or Blockers.
dependencies:
---

# Evidence triage

Decide what a record establishes before writing or relying on Evidence. Keep observations within
what their sources establish and separate empirical results from project authority. Follow the
current task's output and authority contract; in an audit, report findings without writing a Patch.

## Prefer sources in this order

1. Primary artifacts: metrics, manifests, configs, checkpoints, and run outputs.
2. Exact source records containing the result, with timestamps.
3. Explicit human decisions and corrections. These settle framing, not empirical fact.
4. Reviewed synthesis.
5. Assistant summaries.

Use a summary to locate evidence, not as the sole support for an empirical Evidence node.

## Separate observation from interpretation

Write `observation` as what the artifact or record directly states: run, step, value, absence, and
time boundary. Write `interpretation` as what that observation licenses here and, when useful, what
it does not license. Do not use apparatus checks, partial runs, or calibration results to claim
effects they did not test.

If the interpretation merely repeats the observation, consider keeping the information in the
Experiment summary instead of creating Evidence.

When the producing Experiment lists `proxies`, observe the measure and interpret what it licenses
about the quantity it stands for only as far as the proxy holds. Carry the Experiment's
`limitations` that bear on a claim into that edge's qualifications.

## Choose fields deliberately

The graph rules define `origin`, `role`, and `validity`. Decide them from the observation, not from
the claim it will support. A completed calibration can be valid within its measured scope; an
incomplete run may justify only a qualified snapshot and does not establish the missing result.

## Assess each Hypothesis relation

Write one claim-relative `assessment` on every new edge the graph rules say requires one. Calibrate
it honestly for that claim alone: the same Evidence may have different relevance, weight, scope,
and qualifications for different Hypotheses. Historical unassessed relations remain readable, but
never use that compatibility to omit an assessment from a new applicable edge.

## Keep action handoffs separate

- An `informs` or `addresses` edge records that Evidence bears on a gate. The choice or lifecycle
  change it motivates is a separate action under the current task's authority.
- Do not use a smoke or calibration result on downstream science merely because it enables the
  main run.
- Set the `produces` edge's `expectation` by comparing the observation with the `expected_outcomes`
  written before the run, not with a reading formed after seeing the result.

## Check claim boundaries and citations

Do not infer `Hypothesis.scope`; populate it only from that Hypothesis's cited material. Do not turn
a proposal, recommendation, or “should” statement into Evidence.

Read every `source_refs[].excerpt`. Confirm that it contains the claimed observation rather than
merely coming from the same conversation. If one excerpt could support several unrelated Evidence
nodes, it probably supports none of them. Cite the exact source or primary artifact.

Read [worked examples](references/worked-examples.md) when calibrating action Evidence,
claim-relative assessments, qualified snapshots, or citation quality.
