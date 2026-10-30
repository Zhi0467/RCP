# Merge guards and stale Evidence edges

Working design for one PR. Its rules move into the owning specs, and this file
is deleted before merge.

## A. Structural guards for branch merge

**The bug class (v0.4.23).** A field that RCP's engine writes was compared as
content, and the merge check refused every merge.

**What #330 already did.** Each graph field now declares one role
(`GraphField`). `compare_graphs` and the merge oracle look only at
`role="content"` fields. #330 also deleted the old production self-check that
did the blocking.

**What is missing.** Nothing exercises fork Proposals whose filing stamps differ
from the ones the destination restamps. So a field misdeclared as content would
pass CI, then refuse or corrupt real merges.

The guards are tests only. Production gets no self-check back.

### A1. Extend the generated merge test

The sibling `tests/test_graph_invariants.py::test_generated_three_way_merge_preserves_content_through_approval`
already does most of the work:

- forks a real `HistoryManager`
- answers conflicts with every allowed answer kind
- commits through `commit_branch_merge_with_history`
- checks content against `_merge_oracle`, which is independent of the planner
- replays the result

It covers nodes and edges only. Extend it rather than adding a file:

- **Fork Proposals.** On the fork, the generator files a few single-op agent
  Proposals, picking the forms `create_proposals` already generates. Some are
  appended with `rcp.core.materialize.proposal_filing_dependency_rule` patched
  to return 1, which is how a pre-0.4.23 writer stamps them. The merge refiles
  them under rule 2. Multi-op and human-filed Proposals cannot be authored on a
  fork, so they are left out.
- **Glossary.** Upserts on both sides, with distinct terms. Case-variant
  spellings normalize on refile and would be false differences.
- **Oracle.** It also covers `proposals` and `glossary`, comparing each content
  field across base, source and destination, as it already does for nodes and
  edges.
  - A source Proposal that is resolved or stale is left out, as
    `_source_for_comparison` leaves it out.
  - A carried Proposal must keep its content fields.

One fixed regression proves the guard. Annotate `dependency_rule` with role
`content` (the v0.4.23 bug). The extended test must then fail, because the
oracle expects the fork's rule 1 and the merge stored rule 2. This is checked
once during the build and recorded in the PR. It is not a permanent
monkeypatch test; ub-mode's `mutator` covers the general case.

### A2. Populated old-writer branch

`test_server_upgrade.py::test_release_update_from_every_published_release_validates`
already seeds `prepare_data(merge_branch=True)` with each published release's own
code (floor 0.4.25). It then runs the candidate's merge check. Extend that path:

- **The seed.** `supervisor_reboot_data` adds one more agent Patch to the merge
  branch:
  - a glossary upsert
  - new Evidence with a `supports` edge carrying an assessment
  - removal of one seeded edge

  These ops exist at 0.4.25. A write is gated only where a field is newer than
  the floor, using the existing `inspect` / `hasattr` pattern.
- **The check.** `_assert_old_branch_merge_validates` keeps its planning. Then:
  - it commits `build_merge_patch(plan, answers)` through
    `commit_branch_merge_with_history`
  - it asserts that every source edge, glossary term and current Proposal
    content field is present on main, and that replay equals state

  The commit lands in the test's temp copy.

## B. Stale Evidence→Hypothesis edges

### Rule (derived, no stored field)

An edge is **stale** when all of these hold:

- its relation is one of the project type's `belief_outcome_relations`
- its source is a current, non-retired outcome node
- its target is a belief node
- `source.updated_rev > edge.created_rev`

Rewording, Agree/Contest and any other `updated_rev` bump count, the same as
for Proposals. Re-judging recreates the edge, which gives it a new
`created_rev` and clears the mark.

The rule is `stale_edge_ids(state)` in a new `core/validation/edges.py`, using
`project_type_of(state)`.

Merge cases (documented, accepted):

- **Fork rewords Evidence and recreates its edge with different content.** The
  edge is carried, and it lands current.
- **Fork rewords Evidence and re-judges the edge to identical content.** Merge
  compares content only, so the edge is not carried, and it lands stale. One
  Refresh fixes it.
- **Fork judged an edge, and main reworded the Evidence before the merge.** The
  carried edge gets the merge revision, so it lands current.

### Projection

- `GraphAttentionProjection.stale_edge_ids` is the only new field. A stale edge
  has no further refusal, so there is no separate "refreshable" list.
- An older display cache takes the field from the fresh projection: add it to
  the tuple in `projects.py` beside the Proposal keys.
- Web: add it to `types.ts` and `projectTransition.ts`, and pass it to
  `RelationMap` and the DAG.
- Red marks show on any graph target. Refresh is offered only on main, as for
  Proposals.

### Surface

Mockup approved, at desktop and 390px:

- **Node detail `RelationMap`**, on both endpoints: a stale edge's peer box
  gets a red left edge and a red relation pill. One icon-only `RotateCw` button
  (accessible name "Refresh relation") sits beside the pill. A failure turns the
  icon red and the error goes in the title.
- **DAG:** the edge line and its label use `--red`. There is no action on the
  line.
- There is no caption, banner or Inbox row.

### Refresh

`POST /api/projects/{id}/edges/{edge_id:path}/refresh` lives beside the Proposal
route in `api/proposal_refresh.py`. The two routes share one launch helper,
which covers admission, profile, lowest effort, lock and idempotency.

- **Refusal.** It returns 409 `edge_not_stale` when the edge is absent or not
  stale.
- **Key.** `uuid5(NAMESPACE_URL, "rcp:edge-refresh:{edge_id}@{source.updated_rev}")`.
  Repeat clicks reuse the chat, and a later reword gets a new one.
- **Launch.** It starts ordinary node Work on the source Evidence, with
  `evidence-triage` when the project enables it.
- **Context** (`runs/proposal_refresh.py` gains the edge key, skill and message
  builders):
  - the edge's id, relation, assessment and explanation
  - the Evidence's id, title and changed rev
  - the ids of pending Proposals that cite the edge as their cause

**Proposal Refresh re-judges its stale cause edge.** A reword makes both the
edge and any pending Proposal citing it stale. Recreating the edge would make a
freshly refiled Proposal stale again. So:

- When a stale Proposal's Evidence cause edge is itself stale, the Proposal
  refresh context lists that edge.
- The agent first re-judges the edge by recreating or removing it. Then it
  withdraws the Proposal and, if still warranted, refiles it citing the current
  edge, all in one turn.
- Edge Refresh stays edge-only. The build checks that a filing may cite an edge
  recreated in the same Patch.

What edge Refresh may change:

- Re-judge this one edge: recreate it with the same or a revised relation,
  assessment and explanation, or remove it.
- Change nothing else and file no Proposals.

Under `authority-and-proposals.md`, Evidence-to-Hypothesis edges are direct
assertions. Hypothesis status stays the protected judgment (invariant 3b).
Like Proposal refresh, the limit is the ordinary Work contract plus the message;
it grants no extra authority. Tests assert the context data, not the wording.

## Specs to update

- `graph-history-and-transitions.md`: the edge staleness rule and the merge cases.
- `authority-and-proposals.md`: edge refresh, and Proposal refresh re-judging a
  stale cause edge.
- `interface-and-visual-design.md`: the node detail and DAG marks.
- `api-web-and-desktop-projections.md`: the attention field.

## Checks

- Python, focused:
  - `test_graph_invariants.py`
  - `test_server_upgrade.py`, running the exact-base gate for one tag locally
  - attention and edge-rule tests
  - edge refresh route and context tests
  - cache-upgrade test
- Web:
  - `RelationMap` and DAG render tests
  - `npm --prefix web run build`
- Served drive on throwaway data:
  - reword Evidence
  - the red row and line appear
  - Refresh opens the chat
  - the mark clears after re-judging
  - repeat at 390px
