# Deterministic branch merge

Date: 2026-09-08
Status: not started. The scope reopening and the `source_refs` merge deadlock
fix landed in [PR #102](https://github.com/Zhi0467/RCP/pull/102); this is the
follow-up implementation. Nothing here is built yet. The decision to put the
deterministic core first is settled in
[the graph-branch scope decision](../decisions/2026-09-08-graph-branch-scope-is-reopened.md).

## The problem

`validate_branch_merge_candidate_conformance`
([branch_merge.py:869](../../src/rcp/runs/branch_merge.py)) already computes the
answer it grades against:

```python
allowed   = _graph_semantic_write_paths(merge_base, merge_branch)   # branch changed it
mandatory = allowed - conflict_covered                              # main did not
missing   = [p for p in mandatory if branch_value(p) != result_value(p)]
```

That is a pointwise specification of the required output, held before the
provider launches. Nothing builds a Patch from it. The only producer is the
agent, which then spends paid minutes reconstructing a set difference RCP
already has, and is rejected when it drops one path.

## What to build

One function, one branch in the existing run loop. No new module layer, no
registry, no strategy objects.

**1. `build_deterministic_merge_ops(context) -> (ops, residue)`** in
`runs/branch_merge.py`, beside the existing path helpers.

Compute `mandatory` exactly as the validator does, then map each path by shape.
`_graph_semantic_write_paths` ([branch_merge.py:1974](../../src/rcp/runs/branch_merge.py))
emits `(collection, identity, "$")` for a whole entity present on one side only,
and `(collection, identity, field, ...)` for a field change:

| Path | Operation |
| --- | --- |
| `("nodes", id, "$")`, on branch only | `create_nodes` with the branch node |
| `("nodes", id, field...)`, ordinary node | `update_nodes`, `changes={field: branch value}` |
| `("edges", key, "$")` | `create_edges` / `remove_edges` |

Everything else is residue: conflicts, protected ResearchQuestion and Hypothesis
changes, node removals, Proposal carrying, and the `project_truth_scope` /
`ontology` / `coverage` globals. Residue is what the agent is for. Read the value
with the existing `_semantic_path_value` ([branch_merge.py:2083](../../src/rcp/runs/branch_merge.py))
so the builder and the validator cannot disagree about what a path means.

**2. Two call sites in `stream_branch_merge_run`**
([branch_merge.py:1430](../../src/rcp/runs/branch_merge.py)):

- Empty residue: commit the built ops through the unchanged
  `prepare_branch_merge_with_history` / `commit_branch_merge_with_history` pair
  and return, with no provider turn. This is the case that must get fast.
- Non-empty residue: stage the built ops with the contract and ask the agent to
  add only the residue. The correction loop, the rebase loop, and
  `PATCH_CORRECTION_MAX_ROUNDS` stay as they are.

**3. Validation does not change.** `validate_branch_merge_candidate_conformance`
stays the single gate for both paths. A deterministic build that cannot satisfy
it is a builder bug and must fail closed, never fall back to a silent agent
merge.

## Deliberately excluded

- No conflict resolution heuristics. A conflict goes to the agent or to a human.
- No new patch kind, no `surface` selector, no plugin boundary. Merge is the
  concrete owner of this policy.
- No change to `_review_proposal_coverage`, the review contract, or authority.
- No change to the merge receipt or its provenance.
- No branch manager UI. That is later work under the scope decision.

## Verification

- A merge whose branch changes are all non-conflicting and ordinary commits with
  zero provider turns, asserted by a launcher that fails if called.
- A merge with one conflicting field still reaches the agent, and the built
  operations survive the correction round unchanged.
- Built operations pass `validate_branch_merge_candidate_conformance` for the
  same fixtures the current agent path covers, so the builder and validator are
  proven to agree rather than assumed to.
- Existing `tests/test_branch_merge.py` stays green unchanged; the deterministic
  path is additive.

Close this handoff when the empty-residue merge commits without a provider turn
and [S125](../acceptance/S125-auto-research-graph-branch-merge.md) still passes.
