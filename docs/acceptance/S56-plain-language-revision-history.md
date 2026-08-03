---
id: S56-plain-language-revision-history
status: implemented
tier: hermetic
driver: pytest + browser
covered_by: tests/test_revision_summaries.py, web/tests/projectHistory.test.mjs
invariants: [1, 2]
reported_by: human, 2026-08-03
last_passed: 2026-08-04
---

# Read what changed between graph revisions

Confirmed by the human through
[`docs/handoff-ui-fixes-and-graph-skills.md`](../handoff-ui-fixes-and-graph-skills.md)
on 2026-08-03.

Overview explains the latest graph revision in ordinary language. Project
History presents the same deterministic summaries by revision alongside access
to Agent tasks. Overview loads only its current-revision summary after project
state is available; the complete summary list is loaded when History opens, so
history projection never delays the project becoming usable.

Agent and human patch producers are responsible for good prose. Rendering then
resolves an identifier only when it maps to a research title known at that
revision, derives a truthful title-based fallback from operations when
`change_summary` is empty, and quotes an already-authored Proposal consequence
when present. Unknown slash-delimited text, including repository paths, remains
literal. The producer contract forbids operation names and inventory-style
counts in new prose; the renderer preserves legacy authored content rather than
silently deleting a real change. It never invents a cross-node causal narrative.

## UI path

1. Open a project at revision 5 whose previous revision has a non-empty
   `change_summary` containing node ids.
2. Read **What changed?** on Overview.
3. Open project History and read revisions 4 and 5, then inspect an Agent task
   from the same drawer.
4. Repeat with a patch whose `change_summary` is empty.

## Assert

- Overview labels the comparison as revision 4 to revision 5 and shows one or
  more readable sentences.
- Project History shows the same revision-5 prose and retains full Agent task
  access.
- Overview requests revision 5 only and does not wait for the complete History
  projection; opening History loads all revisions and shows a truthful loading
  state beside the already-available Agent tasks.
- Known graph ids render as titles and operation names are made reader-facing.
  Repository paths such as `configs/routes.yaml` remain byte-for-byte intact.
- Legacy inventory prose remains visible rather than being discarded. New
  agent and human producers do not author inventory-style summaries.
- An empty `change_summary` still produces a truthful sentence naming affected
  research concepts.
- Proposal consequences are quoted only when stored in the patch; no new causal
  claim is inferred.
- Every summary is derived from append-only patch history and materialized
  state; it is not another authored canonical file.

## Failure means

History reads like an internal database diff, an old patch cannot be understood,
or a deterministic renderer fabricates scientific causality.
