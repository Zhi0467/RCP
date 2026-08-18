# Claim-relative Evidence assessments implementation handoff

Date: 2026-08-17
Status: confirmed and ready to implement after typed graph operations

## Purpose

Separate intrinsic properties of an Evidence observation from how strongly and how directly that observation bears on a particular Hypothesis.

The current `Evidence.strength` field mixes unlike concepts: `diagnostic` is a methodological role, while `preliminary`, `supporting`, and `confirmatory` read as ordinal claim-level weight. It also forces one Evidence node to appear equally strong against every Hypothesis it touches.

This is a graph-schema correction. It does not change the existing rule that agents may attach Evidence directly while changes to an existing Hypothesis itself remain Proposal-only.

## Confirmed model

### Evidence node owns the observation

A current Evidence node owns:

- `observation`;
- `interpretation`;
- provenance, source references, and artifacts;
- `origin`;
- methodological `validity`; and
- a small methodological `role` vocabulary: `result | diagnostic`.

Remove the current live authorable `strength` field. Do not replace it with another node-global confidence or evidential-weight field.

`result` is the default for an ordinary empirical, analytic, or external observation. `diagnostic` means the observation primarily localizes, disambiguates, or debugs a phenomenon. The role does not state how much the Evidence supports or weakens any particular claim.

### Evidence-to-Hypothesis edge owns the assessment

For an epistemic edge whose source is Evidence and target is Hypothesis, attach one typed claim-relative assessment:

- `relevance`: `direct | indirect | contextual`;
- `weight`: `limited | moderate | strong`;
- `scope`: optional bounded text identifying the population, regime, condition, subclaim, or setting covered; and
- `qualifications`: a bounded list of limitations or caveats.

The relation name continues to state direction:

- `supports`;
- `weakens`;
- `refutes`;
- `inconclusive`; or
- Evidence-sourced `contradicts`.

Do not duplicate direction in the assessment. Hypothesis-to-Hypothesis `contradicts` edges carry no Evidence assessment.

This handoff does not redesign Evidence-to-Decision `informs`, Evidence-to-Blocker `addresses`, or custom ontology relations.

Every newly admitted Evidence-to-Hypothesis epistemic edge requires a current assessment. Historical edges without one remain readable and are presented as unassessed legacy relations until an ordinary later edit replaces or enriches them.

## Authority remains unchanged

An assessment is part of attaching/interpreting Evidence against a claim. It does not directly change the Hypothesis record.

Preserve current authority exactly:

- ordinary agents and the Auto-research orchestrator may create, remove, or replace Evidence-to-Hypothesis epistemic edges directly under the existing edge authority;
- they may supply or revise the claim-relative assessment through that same edge-change path;
- changing an existing Hypothesis's statement, rationale, status, standing, scope, predictions, or protected structure remains Proposal-only for agents;
- no agent may approve a Proposal; and
- the transition manager must not infer a Hypothesis status or Decision choice from assessment weight.

Do not route Evidence assessment changes through the Inbox merely because they are epistemic. The existing protected-belief boundary is the Hypothesis record and protected structural relations, not Evidence attachment.

If the current operation vocabulary has no mutable edge-update operation, use the existing remove-and-create edge semantics. Do not add a hidden mutable side channel solely for assessment fields.

## Historical compatibility

Historical Patch bytes and append-only history remain untouched.

The centralized compatibility decoder accepts old Evidence records containing `strength`:

- `strength = diagnostic` becomes current in-memory `role = diagnostic`;
- all other legacy strength values become current in-memory `role = result`; and
- the exact old value is retained as clearly labelled compatibility metadata, provisionally `legacy_strength`, for history/detail display only.

Do not silently map `preliminary`, `supporting`, or `confirmatory` to current edge weight. The historical node-global label does not contain enough claim-relative information for a faithful conversion.

For a historical Evidence-to-Hypothesis edge with no assessment:

- preserve the edge and relation exactly;
- expose it as an unassessed legacy relation;
- do not synthesize current relevance, weight, scope, or qualifications; and
- do not block project open or replay.

Rules for current writes:

- new Evidence creation/update cannot set `strength` or `legacy_strength`;
- replay may carry compatibility metadata in memory;
- no migration revision is appended;
- no historical Patch file is rewritten; and
- a later ordinary edge edit may create a current assessed relation without rewriting prior history.

An older RCP encountering the newer schema follows the repository-wide newer-generation read-only/update-required rule.

## Typed operation and edge schema

Build this change on the typed graph-operation handoff.

Add a strict optional assessment field to the current Edge model, valid only for the Evidence-source epistemic relations listed above. New `create_edges` payloads for those relations require it. Other edge types reject it.

Validation must enforce:

- correct Evidence and Hypothesis endpoints;
- required assessment on new applicable edges;
- no assessment on non-applicable or Hypothesis-to-Hypothesis relations;
- bounded `scope` and qualification lengths;
- normalized, nonblank, nonduplicated qualifications; and
- strict enum values with no unrecognized fields.

Keep the serialized relation and assessment readable. Do not encode the assessment in `explanation` or an opaque metadata dictionary.

## Presentation and agent contract

Update current producers and views so the model is visible where it belongs:

- Evidence node cards/details show observation, interpretation, role, validity, origin, provenance, and artifacts;
- a historical node may show the old strength only as labelled legacy metadata;
- Hypothesis relation/detail views show each Evidence edge's direction, relevance, weight, scope, and qualifications;
- the same Evidence may carry different assessments toward different Hypotheses;
- agent schemas and graph instructions require the assessment on new applicable edges and explain that the relation states direction while the assessment states directness and weight; and
- Research Markdown and history summaries render the assessment beside the relevant edge rather than once on the Evidence node.

Do not add a scoring dashboard, aggregate confidence number, or automatic combination rule across several Evidence edges.

## Transition-manager integration

An Evidence assessment change is an upstream semantic change. The transition manager may mark causally downstream Experiment guidance stale where an Experiment tests or depends on the affected Hypothesis.

It must not automatically:

- change Hypothesis status or standing;
- choose or queue a Decision;
- create a Proposal;
- alter Evidence validity; or
- infer a numerical belief score.

## Important seams

Expected shared-contract seams include:

- Evidence and Edge models under `src/rcp/core/`;
- typed edge operation payloads;
- centralized compatibility decoding;
- relation, Patch, and Proposal validation;
- authority classification for edge replacement;
- materialization and replay;
- research Markdown, history delta, and revision prose;
- `src/rcp/agents/schema.py` and graph prompts;
- API/Web graph types; and
- Evidence/Hypothesis detail and relation presentation.

Land backend schema and compatibility serially before Web type/component work. Preserve all existing unrelated graph and UI behavior.

## Acceptance documentation

Do not automatically create a new scenario. Update the retained grounded-belief or ontology acceptance contract only if it remains active after the documentation archive pass and needs to state this durable promise:

> Evidence records what was observed, its methodological role, and whether it is valid. Direction, relevance, weight, scope, and qualifications are stated separately for each Hypothesis the Evidence bears on. The same Evidence may strongly address one scoped claim and weakly or contextually bear on another, while historical global strength labels remain readable and are never silently reinterpreted.

Otherwise the current specification plus focused compatibility, validation, API, rendering, and browser tests is sufficient.

## Verification

Prove at minimum:

1. New Evidence rejects node-global `strength` and accepts only the current role/validity/origin model.
2. Every old strength value replays without a write; `diagnostic` preserves diagnostic role and all exact old values remain labelled compatibility metadata.
3. No old ordinal value is silently converted into current claim-relative weight.
4. New Evidence-to-Hypothesis epistemic edges require a strict assessment.
5. Other relation types and Hypothesis-to-Hypothesis `contradicts` reject the assessment.
6. One Evidence node can bear differently on two Hypotheses through distinct assessments.
7. Direct agent/orchestrator Evidence-edge authority remains unchanged, while direct existing-Hypothesis edits remain forbidden.
8. Remove-and-create edge replacement remains append-only and attributable.
9. Research Markdown, revision summaries, API, and served Web detail place assessment information on the relation and remove the current global strength badge.
10. Historical unassessed edges remain readable and clearly legacy without blocking replay.
11. Transition invalidation tests treat an assessment edit as upstream semantic change without changing the Hypothesis automatically.
12. Focused backend/Web suites, prior-generation fixtures, full suites, and pre-commit pass.

## Non-goals

Do not:

- redesign the entire ontology;
- add probabilities or numerical confidence scores;
- aggregate Evidence weights into a Hypothesis verdict;
- change the existing protected-belief authority boundary;
- migrate or rewrite old Patch files;
- add assessment fields to every relation; or
- combine this work with a general graph UI redesign.

## Completion

Update the current graph/ontology and authority specifications, then archive this handoff during the final documentation pass after all compatibility and user-facing rendering checks pass.
