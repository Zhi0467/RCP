---
id: S57-fixed-product-ontology
status: implemented
tier: hermetic
driver: pytest + browser
covered_by: tests/test_ontology_evolution.py, tests/test_sync.py, web/tests/attentionRunsOntology.test.mjs
invariants: [1, 2, 3]
reported_by: human, 2026-08-03
last_passed: 2026-08-03
---

# Existing ontology extensions remain readable without a schema editor

Confirmed by the human on 2026-08-03.

The six shipped node types are RCP's authoring product. Project Settings does not
offer custom type, field, or relation editing. This is only a removal of the UI
surface: ontology extensions already present in append-only history continue to
materialize, validate, project onto the base types, render, and replay.

## Drive

1. Open Settings and inspect every section.
2. Open a fixture whose existing history defines and uses custom types, fields,
   and relations.
3. Open its Research, DAG, and node detail views, then replay its history.

## Assert

- No ontology editor or ontology authoring control is present in Settings or
  elsewhere.
- Existing custom nodes, fields, and relations still render.
- Replay remains identical and validates against the ontology at each revision.
- Backend ontology models and historical operations are unchanged.

## Failure means

Removing a researcher-facing schema tool makes an existing research graph
unreadable or silently changes its history.
