---
id: S87-experiment-prerequisite-chains
status: implemented
tier: live
driver: pytest + api
covered_by:
  - tests/test_ontology_v05.py::test_relation_spec_covers_every_relation_flags_mismatches_and_serializes_layer
  - tests/test_prompts.py::test_graph_contract_keeps_fanout_and_points_to_payload_files
  - tests/test_prompts.py::test_experiment_work_contract_explains_the_bound_loop_and_watcher_handoff
  - tests/test_prompts.py::test_structured_invocation_activates_exact_pointer_without_rewriting_human_message
  - tests/test_chat_prompt_protocol.py::test_contract_version_change_rebootstraps_an_existing_native_chat
  - tests/test_control.py::test_loop_may_hand_new_evidence_to_existing_decisions_and_blockers
  - tests/test_experiment_loop_agent_io.py::test_experiment_watcher_wake_keeps_packages_available_without_reinvoking_them
  - tests/test_api.py::test_generic_watcher_wake_keeps_packages_available_without_reinvoking_them
  - tests/test_skill_registry.py::test_experiment_causality_resolves_and_stages_as_an_official_skill
  - tests/test_skill_registry.py::test_official_skills_match_the_action_evidence_ontology
last_passed: 2026-08-08 — full backend and web suites; a fresh-agent generic
  forward audit found reversed, self-blocking, prose-only, duplicate, stale,
  human-only, and external gates; real Codex Work turns loaded the explicit
  package, reported the same defects through the UI, and applied a validator-clean
  repair at revision 8 on a throwaway project while leaving the Decision open and
  unselected; browser console and server log were clean
invariants: [3, 4, 4b, 10b, 10d]
reported_by: human, 2026-08-08
---

# Construct causal action chains around experiments

This scenario was confirmed by the human on 2026-08-08.

The graph may already contain good ResearchQuestions and Hypotheses while its
action structure is wrong. The problem here is not generating more scientific
framing. It is correctly constructing and linking Decisions, Blockers,
Experiments, and Evidence around the work that should run.

For a main Experiment, the agent must identify the Decisions and Blockers that
actually govern whether it can run. It must then ask what settles each one. When
the answer is empirical, the graph must include the precursor smoke,
calibration, profiling, diagnostic, or feasibility Experiment and the Evidence
that carries its result into the downstream Decision or Blocker.

The conceptual causal order is:

```text
real precursor gates
  -> precursor Experiment
  -> Evidence
  -> downstream Decision or Blocker
  -> main Experiment
```

The stored edge directions follow RCP's relation vocabulary:

- the precursor Experiment is `governed_by` only its real input Decisions and
  is `blocked_by` only its real input Blockers;
- the precursor Experiment `produces` its Evidence;
- the Evidence uses `informs` for the downstream Decision or `addresses` for the
  downstream Blocker;
- the main Experiment is `governed_by` that downstream Decision or `blocked_by`
  that downstream Blocker.

A downstream Decision or Blocker that the precursor is meant to settle must
not point backward as a prerequisite of that precursor. `informs` means that
Evidence bears on a human-owned Decision without selecting it. `addresses`
means that Evidence bears on whether a Blocker is cleared, preserved, or
narrowed; the Blocker's status and the edge explanation record the actual
consequence. These are required base graph semantics in this change, not
deferred ontology questions and not project-custom workarounds.

## Two-level authoring behavior

The local prompt and selected skill ask different questions. The local prompt
checks the causal meaning of the Patch being written. The skill checks whether
the wider experiment program is causally complete.

### Local causal check — always-present prompt

Before finishing any graph Patch that creates or materially changes an
Experiment, Decision, Blocker, Evidence node, or an edge among them, the agent
asks:

1. **What must already be true before this Experiment can run?** Attach only
   genuine input Decisions and Blockers.
2. **What will this Experiment determine or unblock?** Those are downstream
   outputs, not prerequisites of this Experiment.
3. **What Evidence does the Experiment produce?** Connect the Experiment to the
   durable observation rather than jumping directly from Experiment to a later
   Decision or Blocker.
4. **Which Decision does that Evidence inform, or which Blocker does it resolve,
   preserve, or narrow?** Record that handoff with `informs` or `addresses`.
5. **Does each edge point in the direction declared by its relation and tell the
   same causal story as the node prose?** In particular, reject a downstream
   Decision or Blocker that has been attached backward to the Experiment meant
   to settle it.
6. **For every Decision or Blocker attached to a main Experiment, what settles
   it?** If the answer is empirical, verify that the precursor Experiment,
   produced Evidence, and downstream handoff all exist in the Patch or current
   graph.

These questions apply to every contract that may originate or correct a
semantic Patch: Seed, Refresh, graph-capable Work, Experiment-loop turns, and
their Patch-correction instructions. Discuss and Paper remain excluded because
they have no graph-change channel. Correction instructions may point back to the
retained contract rather than repeat all six questions, but they explicitly
require the corrected Patch to pass the same local causal check. The check does
not require the optional global skill.

### Global causal-closure check — selected skill

The human-selectable **Experiment causality** skill starts from each main or
next Experiment and asks:

1. Which Decisions and Blockers genuinely gate this Experiment?
2. For each gate, is it settled by a human choice, external availability,
   ordinary operational work, existing Evidence, or a new empirical result?
3. If it needs a new empirical result, what is the smallest bounded precursor
   Experiment, what Evidence will it produce, and how will each relevant result
   affect the downstream Decision or Blocker?
4. What genuine Decisions or Blockers gate that precursor? Recurse through
   those prerequisites without moving its downstream outputs backward.
5. Does every empirical gate have a complete
   `Experiment -> Evidence -> Decision|Blocker` handoff, and does every main
   Experiment depend on the resulting Decision or Blocker?
6. Is any dependency present only in summary prose, duplicated under several
   nodes, circular, self-blocking, stale, or pointed in the wrong direction?
7. Did the proposed repair reuse existing node identities and preserve the
   project's existing ResearchQuestions and Hypotheses unless the evidence
   independently requires changing them?

The skill repairs action-graph causality. It does not turn every human choice,
external outage, repository task, or retry into an Experiment, and it does not
change human authority over Decisions.

## Skill activation and composition

The local causal check is not packaged as a skill. It remains short and is
embedded in the graph-authoring contract, so every Seed, Refresh,
graph-capable Work, Experiment-loop, and Patch-correction turn receives it even
when the project has enabled no packages. This is the primary protection against
locally reversed or incomplete action chains.

RCP must not add a vague instruction to “probably run the skills after a graph
change.” Different packages belong at different points, and a blanket post-pass
would make read-only audits run during ordinary authoring while loading every
skill body into context. Instead the task contract distinguishes:

- **Invoked this turn:** the exact workflow or skill ids already captured from
  the slash picker. The human message and its slash text remain byte-for-byte
  unchanged. In addition, the turn envelope names the invoked package and its
  staged pointer in one short activation sentence requiring the agent to read
  and follow it for this turn. RCP does not rely on the provider noticing or
  interpreting the unchanged slash token by itself.
- **Available when matched:** every other Settings-enabled package remains a
  compact description and folder pointer. Before acting, the agent compares the
  task and intended graph changes with those descriptions and reads only the
  packages whose stated trigger matches.

The package descriptions therefore carry precise activation boundaries:

- **Evidence triage** applies before creating or materially updating Evidence,
  including Evidence that `informs` a Decision or `addresses` a Blocker.
- **Experiment causality** applies when Seed, Refresh, or Work constructs,
  repairs, or globally checks an experiment action program. It is read before
  authoring; after drafting, its closure questions are rerun on the candidate
  Patch.
- **Graph audit** remains a deliberate read-only structural audit. It does not
  automatically run after every graph-writing task merely because it is staged.
- **Research graph audit** remains a deliberate full-audit workflow rather than
  a default tail step of Seed or Refresh.

This keeps Settings selection as availability, slash selection as explicit
invocation with an unchanged human message, and description matching as
automatic use when the task genuinely calls for a selected package. None
changes task authority.

The packages compose without duplicating the local prompt:

1. **Graph audit** checks the whole graph's claims, identities, relations, and
   lifecycle consistency. Its orphan rule is updated for the current ontology:
   an Experiment is not defective merely because it lacks a Hypothesis or a
   governing Decision; the question is whether it has a truthful role through
   `tests`, `produces`, or an action-gate chain. It also stops describing every
   status edit as human-only.
2. **Experiment causality** performs the deeper recursive check over main
   Experiments, precursor Experiments, Evidence, Decisions, and Blockers. When
   used inside a read-only workflow it reports; when used by a graph-capable
   task that asked for repair it may contribute to that task's one Patch.
3. **Evidence triage** checks whether each load-bearing Evidence node—including
   action Evidence—has honest provenance, strength, validity, and interpretation.
   It distinguishes “this result informs a choice” from the human act of making
   that choice, and “this result addresses a blocker” from the Blocker's recorded
   lifecycle status.
4. **Research graph audit** upgrades its dependency closure to all three skills
   and runs them in that order: broad structural reading, action-chain closure,
   then narrow provenance review of the claims and gates the graph relies on.

The existing skill bodies and their worked examples are revised and versioned
with this ontology change. Examples are generic and must not teach a smoke test
to support a downstream scientific Hypothesis merely because the main
Experiment depends on that smoke test.

## UI path

1. In Project Settings, select **Experiment causality** through the existing
   package picker. This adds no new UI control.
2. Open the main Experiment or a project chat, switch the turn to Work, and ask
   the agent to construct or repair the action graph for the experiment program.
3. The agent performs the global causal-closure check, writes one minimal Patch,
   and applies the always-present local causal check before validation.
4. Inspect the DAG. The precursor Experiment, its Evidence, the downstream
   Decision or Blocker, and the main Experiment form one visible causal chain.

## Setup

Use generic fixtures containing:

- a main Experiment gated by a Decision that requires calibration Evidence;
- a main Experiment gated by a Blocker that requires smoke-test Evidence;
- a precursor Experiment with a separate, genuine upstream gate of its own;
- an open Decision that ordinary Work cannot decide and an external Blocker that
  require no invented Experiment;
- an initially wrong graph where the downstream Decision governs the smoke test
  meant to determine it;
- an initially incomplete graph where a main Experiment's gate exists only in
  prose or lacks its precursor-Evidence handoff.

No fixture or acceptance wording names or depends on a particular user project.

## Drive

1. Exercise the always-present local prompt contract hermetically for Seed,
   Refresh, Work, Experiment-loop, and Patch-correction contracts; confirm it is
   absent from Discuss and Paper.
2. Register and stage **Experiment causality**, and stage the upgraded existing
   skills and workflow through the same packaging path.
3. Exercise explicit invocation and description-matched activation separately;
   verify that unrelated staged packages remain pointers only.
4. Forward-test the staged skill on the generic incomplete and reversed graphs
   with fresh agents that are not told the intended topology.
5. Run one real-provider Work turn through the API against a temporary project,
   inspect the proposed Patch, and validate the resulting graph.

## Assert — pytest

- `base_vocabulary_has_informs_and_addresses_action_handoffs`
- `local_prompt_asks_for_real_inputs_outputs_evidence_and_gate_closure`
- `local_prompt_rejects_a_downstream_gate_as_its_precursors_input`
- `local_prompt_covers_every_patch_origin_and_correction_surface`
- `local_prompt_is_absent_from_discuss_and_paper`
- `explicit_invocation_is_named_separately_from_available_pointers`
- `explicit_invocation_preserves_the_human_slash_text_byte_for_byte`
- `matching_packages_are_read_without_loading_unrelated_packages`
- `experiment_causality_is_registered_staged_and_packaged`
- `existing_skills_match_the_current_action_evidence_ontology`
- `research_graph_audit_composes_structure_causality_then_evidence`
- `skill_traces_every_main_experiment_gate_to_its_resolution_source`
- `skill_recurses_through_real_precursor_prerequisites`
- `skill_checks_hidden_reversed_circular_duplicate_and_stale_dependencies`
- `skill_does_not_invent_experiments_for_human_or_external_gates`
- `skill_preserves_human_decision_authority`

## Assert — forward test and live API

- `downstream_decision_does_not_govern_its_own_calibration`
- `downstream_blocker_does_not_block_the_smoke_test_that_can_clear_it`
- `every_empirical_main_experiment_gate_has_a_precursor_and_evidence_handoff`
- `precursor_experiment_uses_only_its_real_input_gates`
- `evidence_links_to_the_decision_or_blocker_it_affects`
- `human_and_external_gates_do_not_gain_invented_experiments`
- `dependencies_hidden_in_prose_are_made_structural`
- `the_resulting_patch_passes_the_live_validator`
- `no_server_traceback`

## Failure means

The agent can produce a type-valid Patch whose action causality is backward or
incomplete: a Decision or Blocker determined by a precursor is modeled as that
precursor's input; an empirical gate on a main Experiment has no precursor and
Evidence handoff; or a required dependency remains only in prose even though
the graph appears runnable.
