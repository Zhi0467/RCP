---
id: evidence-triage
kind: skill
label: Evidence triage
version: 3.1.0
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

## Choose fields deliberately

- Set `origin` explicitly: `internal_run`, `external_publication`, `external_instance`, `analytic`,
  or `unknown` only when provenance truly cannot be classified.
- Set methodological `role` to `result` for an ordinary empirical, analytic, or external
  observation. Use `diagnostic` when the observation primarily localizes, disambiguates, or debugs
  a phenomenon. Role says what kind of observation this is, not how strongly it bears on a claim.
  Never author the retired node-global `strength` or replay-only `legacy_strength` fields.
- Set `validity` to `valid`, `qualified`, `invalid`, or `superseded` based on the observation's
  methodological limits. A completed calibration can be valid within its measured scope. An
  incomplete run may justify only a qualified snapshot; it does not establish the missing result.

## Assess each Hypothesis relation

For every new Evidence-to-Hypothesis `supports`, `weakens`, `refutes`, `inconclusive`, or
Evidence-sourced `contradicts` edge, write one claim-relative `assessment`:

- `relevance`: `direct`, `indirect`, or `contextual`;
- `weight`: `limited`, `moderate`, or `strong`;
- optional `scope`: the bounded population, regime, condition, subclaim, or setting covered; and
- `qualifications`: concrete limitations or caveats, with an empty list only when none apply.

The relation states direction; do not repeat support or opposition inside the assessment. The same
Evidence may have different relevance, weight, scope, and qualifications for different Hypotheses.
Historical unassessed relations remain readable, but never use that compatibility to omit an
assessment from a new applicable edge. Do not attach an Evidence assessment to a
Hypothesis-to-Hypothesis `contradicts` edge or any action, seam, meta, or custom relation.

## Preserve action semantics and authority

- Use `informs` when Evidence bears on a Decision. The edge does not select an option or close the
  Decision; selection is a separate action under the current task's authority. It carries no
  Evidence-to-Hypothesis assessment.
- Use `addresses` when Evidence bears on whether a Blocker is cleared, preserved, or narrowed. The
  edge does not itself change Blocker status; the lifecycle record carries that consequence. It
  carries no Evidence-to-Hypothesis assessment.
- Use `supports`, `weakens`, `refutes`, `inconclusive`, or `contradicts` only when the Evidence
  bears on a Hypothesis, and calibrate its claim-relative assessment honestly. Do not use a smoke
  or calibration result on downstream science merely because it enables the main run.
- Keep Experiment `produces` Evidence separate from the Evidence handoff to a Decision or Blocker.
  Before the result exists, record the intended observation in the Experiment plan, not an Evidence
  node or a `produces` edge.

## Check claim boundaries and citations

Do not infer `Hypothesis.scope`; populate it only from that Hypothesis's cited material. Do not turn
a proposal, recommendation, or “should” statement into Evidence.

Read every `source_refs[].excerpt`. Confirm that it contains the claimed observation rather than
merely coming from the same conversation. If one excerpt could support several unrelated Evidence
nodes, it probably supports none of them. Cite the exact source or primary artifact.

Read [worked examples](references/worked-examples.md) when calibrating action Evidence,
claim-relative assessments, qualified snapshots, or citation quality.
