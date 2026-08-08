---
id: evidence-triage
kind: skill
label: Evidence triage
version: 3.0.0
description: Triage Evidence before creating or materially updating it, or audit load bearing Evidence for provenance, strength, validity, claim boundaries, and action handoffs to Decisions or Blockers.
dependencies:
---

# Evidence triage

Decide what a record establishes before writing or relying on Evidence. Keep observations narrower
than their sources and separate empirical results from project authority.

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
it does not license. Do not promote apparatus checks, partial runs, or calibration results into
scientific conclusions.

If the interpretation merely repeats the observation, consider keeping the information in the
Experiment summary instead of creating Evidence.

## Choose fields deliberately

- Set `origin` explicitly: `internal_run`, `external_publication`, `external_instance`, `analytic`,
  or `unknown` only when provenance truly cannot be classified.
- Set `strength` to `diagnostic`, `preliminary`, `supporting`, or `confirmatory` according to the
  exact claim the Evidence bears on. A smoke test can be confirmatory about apparatus readiness and
  still say nothing about a scientific Hypothesis.
- Set `validity` to `valid`, `qualified`, `invalid`, or `superseded`. Use `qualified` when the
  interpretation contains a material boundary such as “only,” “pending,” or “still required.”

## Preserve action semantics and authority

- Use `informs` when Evidence bears on a Decision. The edge does not select an option or close the
  Decision; record the human selection separately through the authorized path.
- Use `addresses` when Evidence bears on whether a Blocker is cleared, preserved, or narrowed. The
  edge does not itself change Blocker status; the lifecycle record carries that consequence.
- Use `supports`, `weakens`, `refutes`, `inconclusive`, or `contradicts` only when the Evidence
  directly bears on a Hypothesis. Do not use a smoke or calibration result on downstream science
  merely because it enables the main run.
- Keep Experiment `produces` Evidence separate from the Evidence handoff to a Decision or Blocker.

## Check claim boundaries and citations

Do not infer `Hypothesis.scope`; populate it only from that Hypothesis's cited material. Do not turn
a proposal, recommendation, or “should” statement into Evidence.

Read every `source_refs[].excerpt`. Confirm that it contains the claimed observation rather than
merely coming from the same conversation. If one excerpt could support several unrelated Evidence
nodes, it probably supports none of them. Cite the exact source or primary artifact.

Read [worked examples](references/worked-examples.md) when calibrating action Evidence, preliminary
snapshots, or citation quality.
