---
id: S132-child-experiments-appear-in-runs
status: pending
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_experiment_index.py
  - web/tests/experimentBoard.test.mjs
  - web/tests/experimentRunDetail.test.mjs
  - web/tests/experimentBoard.browser.test.mjs
invariants: [8, 10g]
---

# A dispatched child Experiment appears in Runs

An Auto-research episode may dispatch a child Experiment on its own graph
branch. That child is one lifecycle with its own budget and controls, and it
must be visible in project **Runs** before anyone opens its exact route. The
parent's card links to it as provenance; it never becomes a second budget or a
second Stop. The budget and Stop contract itself remains
[S78](S78-one-budget-one-stop.md).

Hermetic backend and web coverage exists. The served browser drive below with a
real dispatched child has not been run, so this scenario stays pending.

## Drive — browser

1. Start auto-research from the project header and let the orchestrator
   dispatch a child Experiment on the episode's graph branch.
2. Open **Runs**. Find the child both as a linked subordinate **Experiment**
   turn beneath the parent's Turns list and as its own Experiment-loop card
   with its separate budget and controls, before opening its exact route.
3. Expand the child card. Its detail names the active turn, shows the
   Experiment objective apart from any stale prior summary or next action, and
   says the owning episode is watching its completion only when a matching
   completion watcher exists.
4. With the child's exact route in the URL, collapse the card, then reopen it
   once through the card toggle and once through the nested Turns link. The
   transcript returns each time and the URL does not change.
5. Make another project in the space unavailable. **Runs** for the healthy
   project keeps refreshing without an error notice.

## Assert — pytest + browser

- `a_dispatched_experiment_is_linked_under_its_parent_and_keeps_its_own_run_card`
- `child_detail_names_its_active_turn_and_separates_objective_from_stale_guidance`
- `parent_watching_is_published_only_for_a_completion_watcher`
- `reopening_an_indexed_child_restores_its_selection_without_a_hashchange`
- `runs_refresh_is_scoped_to_the_open_project`
- `no_console_or_application_request_errors`
