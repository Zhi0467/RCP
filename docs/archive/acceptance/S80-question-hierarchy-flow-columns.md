---
id: S80-question-hierarchy-flow-columns
status: implemented
tier: hermetic
driver: browser
covered_by:
  - web/tests/dagLayout.test.mjs
  - browser 2026-08-07 — a root, child, and grandchild rendered in three
    successive question columns before the later semantic stages; both layout
    modes remained selectable and browser/server diagnostics were clean
last_passed: 2026-08-07
invariants: []
---

# Read question hierarchy from the Research flow columns

Research flow uses horizontal position to show both the hierarchy between
Research Questions and the later stages of research. A root question begins the
flow. Each `has_subquestion` level advances one column, and Hypotheses and
Decisions begin only after the deepest visible question level.

## UI path

- Open a project whose graph has a root Research Question, two levels of
  subquestions, and the ordinary Hypothesis, Decision, Experiment, Blocker, and
  Evidence node types.
- Open **Research**, switch to **DAG**, then choose **Research flow**.
- Read the question hierarchy from left to right before following the later
  semantic stages.
- Switch to **Force-directed** and back to confirm this change does not remove
  either layout mode.

Questions with no `has_subquestion` parent are roots. A question with multiple
parents follows its deepest parent. Questions in a cycle share one column.
Relations other than `has_subquestion` do not assign question depth. The later
semantic lanes retain their existing grouping: Hypothesis with Decision,
Experiment with Blocker, then Evidence.

## Assert

- `root_questions_share_the_first_question_column`
- `each_subquestion_level_advances_one_column`
- `multi_parent_questions_follow_their_deepest_parent`
- `cyclic_questions_share_one_column`
- `non_hierarchy_relations_do_not_change_question_depth`
- `later_semantic_stages_begin_after_the_deepest_question`
- `force_directed_layout_remains_available`
- `no_console_errors`

Do not assert pixel coordinates or vertical order. The promise is the horizontal
column assignment and the semantic ordering between columns.

## Failure means

A parent and subquestion collapse into the same Research flow column, later
research stages overlap a deeper question level, or unrelated relation arrows
silently redefine the question hierarchy.
