---
id: S12-ontology-evolution
status: implemented
tier: hermetic
driver: pytest
covered_by: tests/test_ontology_evolution.py, tests/test_sync.py
invariants: [1, 3]
last_passed: 2026-08-08
blueprint: research-control-panel-blueprint.md#schema-evolution-and-transfer-boundary
---

# Keep historical ontology extensions readable

RCP now ships six product node types and does not expose a schema editor. Older
projects may nevertheless contain append-only operations that defined custom
types, fields, and relations. Those records must keep opening, keep meaning what
they meant, and replay identically.

This is a compatibility promise, not a current ontology-authoring path. The
separate browser promise in
[`S57-fixed-product-ontology.md`](S57-fixed-product-ontology.md) removes the
editing surface while preserving the behavior below.

## Historical compatibility

- A project with no ontology keys still opens under the base schema.
- A custom type is interpreted using the ontology in force at the revision that
  created or edited it.
- Later deprecation or removal does not make earlier records unreadable.
- Custom fields and relations already present in history continue to
  materialize and validate.
- Replay remains byte-for-byte faithful to the recorded node and edge fields.
- The base ontology cannot be redefined by a historical or newly submitted
  operation.

Materialization still derives all ontology state from the append-only patch log.
No compatibility code may hand-edit `graph.json` or another derived file.

## What an agent is told

The base vocabulary — node id prefixes, the seventeen base relations and their
endpoints, `Evidence.origin`, `Hypothesis.scope`, and the Experiment/Evidence
connection rules — is how an agent knows what a legal graph edit is. The
relations include `informs` from Evidence to Decision and `addresses` from
Evidence to Blocker. It appears in every patch-producing contract regardless of
ontology state.

The extension-authoring rules describe how to write a custom type, a custom
field, and a custom relation. A project that has never defined one cannot use
them, so they appear only when the project's materialized ontology actually
carries definitions. A project that does carry them still receives them, which
is what keeps this compatibility promise true at the prompt as well as at
replay.

## Drive

1. Replay a legacy project with no ontology or extension keys.
2. Replay a project that defines a custom type, uses it in later revisions, and
   later removes the type.
3. Compare every materialized node and edge field with the recorded graph.
4. Validate existing custom fields and relations against the ontology that was
   active at each revision.
5. Attempt to redefine the base ontology or narrow a historical relation past
   edges that already use it.
6. Build a patch-producing contract for a project with an empty materialized
   ontology and for one carrying historical extensions.

## Assert

- `test_old_project_opens_without_ontology_or_extension_keys`
- `test_legacy_patch_replay_preserves_every_recorded_node_and_edge_field`
- `test_validation_uses_the_ontology_in_force_before_each_patch`
- `test_removed_type_and_fields_remain_readable_during_replay`
- `test_custom_relation_uses_semantic_types_and_materializes_a_crossing_edge_as_seam`
- `test_base_ontology_cannot_be_redefined`
- `test_relation_narrowing_names_the_edges_and_nodes_that_block_it`
- `test_extension_authoring_rules_appear_only_for_a_project_with_extensions`
- `test_base_authoring_rules_appear_regardless_of_ontology_state`

## Failure means

Removing the schema editor made an existing research graph unreadable or
silently changed its history, or an agent working in a project with historical
extensions was never told how to write them.
