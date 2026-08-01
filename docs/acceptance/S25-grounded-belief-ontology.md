---
id: S25-grounded-belief-ontology
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_ontology_v05.py
  - tests/test_agent_schema.py::test_new_agent_evidence_requires_an_explicit_origin
  - tests/test_history.py::test_tampered_accepted_patch_halts_before_it_and_blocks_later_writes
  - web/tests/nodeDetail.test.mjs
  - web/tests/graphProjection.test.mjs
  - web/tests/graphAuthority.test.mjs
last_passed: 2026-07-30
invariants: [1, 2, 3, 7b]
blueprint: v0.5 §5.1, §5.4–§5.5, §6.4
---

# Belief changes are grounded and readable

This is the fixed-ontology v0.5 promise. A graph says what changed a belief and
why, distinguishes where evidence came from, keeps scientific boundary
conditions under explicit human supervision, and lets the same research be read
as belief formation or as action taken to learn.

This scenario does not cover dynamic schema evolution. [S12](S12-ontology-evolution.md)
starts only after this scenario and [S13](S13-replay-halts.md) pass, the fixed
base ontology has survived real graph use, and `RELATION_SPEC` flags have shown
which base mappings are stable. S13 remains the separate replay-failure promise;
**S13 and S25 turning green together define fixed-ontology v0.5.**

## UI path

### Read one scientific object without invented certainty

Opening a Hypothesis shows its statement, status, and **Scope**. Scope contains
the boundary conditions under which the hypothesis is asserted. It is part of
the existing direct node editor, stages in the project draft, and reaches
canonical history only through **Sync**. Editing it never starts chat.

An agent may write Scope only when one of its cited sources states the boundary
explicitly. Otherwise it leaves Scope empty and creates an Ambiguity asking the
human to supply it. Empty is honest; there is no `unknown` filler.

Opening Evidence shows **Origin** as one of `Internal run`, `External
publication`, `External instance`, `Analytic`, or `Unknown`. Origin is a compact
field value, not a helper subtitle. New agent-authored Evidence must name it;
old Evidence written before v0.5 remains readable.

The universal Confidence chip and authoring control appear nowhere. Hypothesis
status and Evidence strength/validity already own epistemic meaning; a second
undefined axis must not survive as display, input, prompt, or newly authored
payload. `confidence` is deleted from the model outright.

### Read why a belief moved

The Hypothesis detail drawer's existing **Status history** shows each status
transition with its revision and cause. The cause links to the Evidence relation,
Decision, Proposal resolution, or human edit that justified it. A status change
without one of those four causes is refused rather than recorded as unexplained
belief.

An edge outside `RELATION_SPEC` is accepted provisionally but visibly flagged.
The relevant detail view names the relation, actual endpoint types, and expected
shape. It is not a generic warning and does not silently remove the edge.

### Read the graph through both layers

Research → DAG keeps **Research flow** as the layout and adds one compact
three-state control: **All / Belief / Action**.

- **All** renders the ordinary complete graph.
- **Belief** emphasizes epistemic and seam relations and dims action-only
  context.
- **Action** emphasizes action and seam relations and dims epistemic-only
  context.

Nothing is deleted or relaid out when the selection changes. `Experiment` and
the `tests` / `produces` seam stay legible in both focused projections. Meta
relations such as `supersedes` and `duplicate_of` remain available as neutral
context. Pins, one-hop focus, fullscreen, trust view, and manual brightness are
independent of the layer selection. There is no second graph, new navigation
destination, or ontology control in the DAG.

### Development reset boundary

RCP is still in development. Fixed-ontology v0.5 establishes a clean schema
baseline for `confidence`: there is no compatibility path for pre-v0.5 payloads
carrying it. Existing development state and fixtures may be reset or reseeded.
Do not add a migration, ignored legacy field, fallback, or drop list solely to
preserve that state. S12's log-authored schema evolution is the compatibility
contract for ontology changes made after this baseline.

## Setup

A temporary v0.5 project containing:

- a Hypothesis with several caused status transitions;
- Evidence from an internal run and an external publication;
- legal epistemic, action, seam, and meta relations;
- one deliberately mistyped but structurally valid relation that should flag;
- an Experiment with several nested attempts.

No real provider or remote machine is needed. Admission checks use patches and
the browser path uses the resulting fixture.

## Drive

1. Open the project and its Research view. Open a Hypothesis and an
   Evidence item.
2. Edit the Hypothesis Scope directly, navigate away and back, then Sync and
   reopen the project.
3. Read the Hypothesis Status history and follow a cause to its related object.
4. Open the deliberately mistyped relation and read its validation flag.
5. Open Research → DAG. Select Belief, then Action, then All.
6. With each projection active, focus a node, pin it, enter fullscreen, and
   return to the prior projection.

## Assert — pytest

- `confidence_is_absent_from_the_model_and_agent_schema`
- `legacy_confidence_payload_is_not_supported`
- `old_evidence_without_origin_replays`
- `new_evidence_requires_explicit_origin` — omission is an authoring rejection,
  not a structural replay failure
- `grounded_agent_scope_is_allowed`
- `ungrounded_agent_scope_is_rejected`
- `missing_scope_can_raise_an_ambiguity`
- `human_scope_edit_is_allowed_and_sync_only`
- `hypothesis_status_change_requires_cause`
- `all_four_cause_kinds_are_valid`
- `same_patch_cause_resolves_after_creates`
- `relation_spec_covers_every_relation`
- `relation_shape_violation_flags_without_rejecting`
- `only_structural_rules_run_on_replay`
- `attempts_remain_nested` — no new Claim, Belief, Observation, or Attempt node
  type appears

## Assert — browser

- `confidence_is_absent_everywhere`
- `scope_is_visible_and_directly_editable`
- `scope_edit_survives_navigation_and_sync`
- `origin_is_visible_on_evidence`
- `status_history_names_and_links_the_cause`
- `relation_shape_flag_explains_the_mismatch`
- `all_belief_action_control_is_visible`
- `belief_projection_emphasizes_epistemic_and_seam_relations`
- `action_projection_emphasizes_action_and_seam_relations`
- `projection_change_does_not_relayout_or_hide_context`
- `experiment_seam_is_legible_in_both_projections`
- `projection_preserves_focus_pins_fullscreen_and_brightness`
- `no_new_ontology_control_or_primary_destination`
- `no_console_or_application_request_errors`

## Failure means

RCP either invents scientific certainty, records a belief change with no reason,
hides where evidence came from, silently presents an incoherent relation as
truth, or declares two research layers that the researcher cannot actually use.
Any one of those turns the ontology back into labels over an experiment tracker.
