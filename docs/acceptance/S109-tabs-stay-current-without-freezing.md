---
id: S109-tabs-stay-current-without-freezing
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_transport.py
  - tests/test_api.py
  - tests/test_api.py::test_cached_revision_heartbeat_enforces_three_second_probe_cooldown
  - web/tests/canonicalRevisionRefresh.test.mjs
  - web/tests/humanDraft.test.mjs
  - web/tests/decisionChoice.test.mjs
  - web/tests/chatWorkspace.test.mjs
  - browser 2026-08-11 — isolated two-project tab, external patch, behind, Apply, Sync
  - browser 2026-08-11 — inactive revision advanced before selection and active revision appeared inside five seconds
  - live SSH 2026-08-11 — unchanged head probe and forced-local-stale reconciliation
  - live SSH 2026-08-11 — 7.5–13.0 ms cache heartbeats, no probe at 2.9 seconds, second unchanged probe at 3.1 seconds
invariants: [1, 3, 6]
last_passed: 2026-08-11 — two isolated open tabs polled at the three-second and
  one-second cadences; an inactive tab fetched revision 8 before selection with
  no loading frame, the active tab displayed revision 9 within five seconds,
  and live SSH enforced the three-second probe cap while heartbeat responses
  remained cache-only
---

# A project tab stays current without ever waiting on the remote

Switching between open projects must never block on SSH, and a project left open
must not silently show a graph that has moved on. Neither of those may cost the
human work they have already typed.

## UI path — confirmed 2026-08-11

- Switching to an already-opened project tab renders that project's previous
  state immediately: graph, tasks, watchers, chat summaries and loaded
  transcripts, revision summaries, view, selection, and scroll. There is no
  blank frame and no spinner between tabs.
- While the app is visible, every open project tab — selected or not — receives
  a cache heartbeat at least every three seconds. The selected tab also checks
  cached revision once per second so a completed reconciliation appears quickly.
- Heartbeats may schedule the remote's cheap patch-head question, but each
  project is capped at one remote probe every three seconds and remains
  single-flight. Switching tabs itself never synchronously starts or waits for
  SSH. Returning to a visible app immediately sweeps the open tabs.
- When an inactive tab's cached revision advances, RCP updates that tab's
  retained snapshot and rebases its draft in the background. Selecting it does
  not become the event that discovers or loads the change. With a healthy,
  reachable remote, a selected tab is expected to be current within roughly
  three to five seconds.
- When the answer is yes, RCP performs the full refresh in the background and
  the tab updates in place. The human presses nothing. There is no Pull
  control, no "remote changed" banner, and no confirmation.
- A staged human draft survives that refresh. Every staged node whose canonical
  node did not move is carried forward untouched and stays committable.
- A staged node that did move is marked **behind** on the node and in its
  drawer. Its staged text stays in the editor, editable. The incoming canonical
  value appears beneath the field it belongs to, read-only, with one **Apply**
  control per field that swaps the two: incoming moves into the editor, the
  replaced text moves into the reference block. Pressing Apply again swaps back.
- Editing a behind field — by typing or by Apply — re-pins that entry and makes
  it committable. An untouched behind entry is never committable.
- Sync counts only committable entries. A behind entry is counted separately and
  is never included in a Sync the human did not resolve. Sync is never disabled
  merely because the graph moved.
- Deliberately not possible: choosing "keep mine" or "take theirs" as a
  strategy, resolving through a modal, discarding the whole draft to recover
  from a moved graph, or committing a behind entry the human has not seen.

## Drive

1. Open two projects as tabs. In the first, stage edits to three nodes without
   syncing, scroll a panel, and open a conversation.
2. Switch to the second project and back. Confirm the first tab returns with its
   staged edits, scroll position, view, and open conversation intact, and that
   no blank frame appears between them.
3. Leave the first tab inactive and append a patch to its canonical state
   repository from outside the app, touching exactly one of the three staged
   nodes.
4. Wait through the open-tab polling window, then switch back. Confirm the tab
   either already holds the new revision or advances within five seconds,
   without the switch synchronously waiting on SSH.
5. Confirm the two untouched staged nodes are unchanged and still committable,
   and the third is marked behind.
6. Open the behind node. Confirm the staged text is still in the editor and the
   incoming value is shown beneath the field. Press Apply, then Apply again.
7. Press Sync. Confirm what commits matches the committable count.

## Assert

- `switching_tabs_issues_no_synchronous_remote_call`
- `returning_tab_restores_graph_tasks_chats_scroll_and_view`
- `returning_tab_never_renders_an_empty_state`
- `every_open_tab_is_polled_while_visible`
- `inactive_tab_cache_advances_before_selection`
- `selected_tab_checks_completed_reconciliation_each_second`
- `remote_probe_is_capped_at_once_per_project_per_three_seconds`
- `visibility_resume_sweeps_every_open_tab`
- `probe_takes_no_canonical_lock_and_copies_nothing`
- `unchanged_remote_head_starts_no_refresh`
- `moved_remote_head_refreshes_without_human_action`
- `refresh_is_single_flight_per_project`
- `switching_away_neither_cancels_nor_duplicates_a_refresh`
- `cached_revision_never_moves_backwards`
- `older_generation_never_overwrites_a_newer_snapshot`
- `staged_nodes_untouched_remotely_survive_the_refresh`
- `staged_node_that_moved_is_marked_behind`
- `behind_entry_is_excluded_from_the_sync_count`
- `untouched_behind_entry_cannot_be_committed`
- `editing_a_behind_field_repins_and_commits`
- `apply_swaps_incoming_and_staged_without_losing_either`
- `sync_is_never_disabled_by_graph_movement`
- `display_cache_never_authorizes_or_supplies_a_canonical_write`

## Failure means

A tab switch waits on SSH, a project sits on a revision that moved hours ago
with no signal, a background refresh silently discards typed edits, or a staged
edit commits over a canonical change the human never saw.

## Notes

The whole-draft `base_revision` pin stops gating node entries; per-node
`base_updated_rev` already carries the safety and the server already enforces it
independently. Ontology remains the one part of a draft with no per-item pin and
may still block a Sync on its own — that exception is deliberate and narrow.

Nothing here is version control. The append-only log stays linear, nothing
branches or merges, and the only thing being reconciled is an uncommitted
client-side edit buffer against state that moved beneath it.
