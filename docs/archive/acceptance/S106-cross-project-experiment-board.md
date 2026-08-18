---
id: S106-cross-project-experiment-board
status: implemented
tier: hermetic
driver: pytest + browser
covered_by: tests/test_experiment_index.py, web/tests/experimentBoard.test.mjs
invariants: [8, 10b, 10g]
last_passed: 2026-08-09 — API and component checks plus a served browser drive
  verified cache-only polling, current grouping, unavailable rows, the narrow
  layout, and the exact expanded Runs deep link with no console or server errors.
---

# See every launched Experiment loop before opening a project

The project index gives the researcher one current operational view across all
registered projects. It lists Experiment nodes that have launched a bounded
loop, not every Experiment in every graph and not every historical episode as a
separate item. One Experiment keeps one row representing its current or latest
episode, so several overnight loops can be checked without opening their
projects one by one.

## UI path

Confirmed by the human on 2026-08-09.

1. Open the project index with several registered projects. The existing project
   cards remain first. A visually distinct **Experiments** board appears below
   them and uses compact horizontal operational cards rather than project-cover
   cards.
2. Give one project an Experiment that has never been run, and give other
   Experiments one or several loop episodes. The never-run Experiment is absent.
   Every launched Experiment appears once, representing its current or latest
   episode rather than duplicating historical episodes.
3. Read the board header. Its counts describe current state, not changes since a
   previous visit. Cards are grouped **Needs action**, then **In progress**, then
   **Finished**; the first two groups stay open and ahead of Finished. Within a
   group the most recently active loop comes first.
4. Inspect compact cards for active, watcher-waiting, degraded, failed,
   interrupted, invocation-limit, stopped, successful, abandoned, and superseded
   loops. Each card names its Experiment and project, reports the existing Runs
   health truth, shows current summary or next action when present, and shows
   last activity. Degraded work that can still continue automatically remains In
   progress with a warning treatment. Work requiring human recovery or
   reauthorization is Needs action. Finished outcomes remain distinguishable as
   **Succeeded**, **Abandoned**, or **Superseded**.
5. Finished contains every launched Experiment whose latest loop is terminal,
   ordered most recent first, but the whole section is folded by default. Expand
   and fold it without changing any loop state.
6. Make a registered project unavailable. Its last-known loop cards remain in
   place and say **Unavailable**; the index neither hides them nor blocks on that
   project. Restore reachability and let loop state change while the index stays
   open. The board refreshes without opening projects individually.
7. Select a loop card. RCP opens its project directly in **Runs** with that
   Experiment's detail expanded. The index card offers no Run, Retry, Stop, or
   other operational action of its own.
8. Open an index whose projects have no launched Experiment loops. The
   **Experiments** board remains present with one compact empty state. Repeat at
   a narrow width: card contents stack without becoming project covers, hiding
   status, or losing keyboard focus.

## Assertions

- `project_index_lists_only_experiments_with_loop_history`
- `one_experiment_has_one_latest_loop_card_across_episodes`
- `board_counts_describe_current_state_without_unread_bookkeeping`
- `needs_action_precedes_in_progress_and_finished_is_folded`
- `loop_cards_reuse_runs_health_and_outcome_semantics`
- `degraded_automatic_work_remains_in_progress_with_a_warning`
- `finished_loops_remain_available_and_keep_distinct_outcomes`
- `unavailable_projects_keep_their_last_known_loop_cards`
- `landing_board_refreshes_without_opening_each_project`
- `selecting_a_loop_opens_its_expanded_runs_detail`
- `landing_loop_cards_offer_no_operational_actions`
- `empty_and_narrow_states_remain_legible_and_accessible`
- `no_console_failed_request_or_server_error_during_the_browser_drive`

## Deliberately not possible

The project index is not a second Runs control surface. It does not list
never-run Experiments, split one Experiment into an episode archive, track an
unread or since-last-visit state, or authorize loop actions outside the project.

## Failure means

The researcher must still open projects one by one to discover loop state, the
index invents a status that disagrees with Runs, historical episodes flood the
board, terminal work disappears, unavailable work is silently hidden, or a
landing-page action changes Experiment control state.
