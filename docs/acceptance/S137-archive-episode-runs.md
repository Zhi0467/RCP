---
id: S137-archive-episode-runs
status: implemented
tier: hermetic
driver: pytest + api + browser
covered_by:
  - tests/test_episode_archive_api.py
  - tests/test_episode_archives_storage.py
  - tests/test_experiment_index.py
  - tests/test_transfer_records.py
  - tests/test_transfer_import_storage.py
  - tests/test_transfer_episode_archive_compatibility.py
  - web/tests/episodeArchive.browser.test.mjs
  - web/tests/spaceRuns.test.mjs
invariants: [1, 3, 9, 10g]
reported_by: human, 2026-09-07
last_passed: >-
  2026-09-07 — a disposable served project exercised Archive and Unarchive for
  a failed child and its Auto-research parent in project and space Runs. Counts,
  nested links, reload persistence, Show archived, and recorded starter profiles
  matched the stored state. All archive requests returned 200, with no browser
  console or server errors. API checks verified shared team visibility and
  membership; regressions covered the former active-work refusal, retained history,
  migration, backup, transfer, older-reader refusal before release, and stale
  browser polls.
---

# Archive an episode without erasing its work

The human confirmed reversible episode archiving on 2026-09-07 and chose to
share the archive with everyone in a team project. Episode cards also identify
the recorded human who started the run through a compact avatar and name.
On 2026-09-09 the human extended Archive to every episode, including active and
Needs Action episodes. Archive continues to change visibility only.

## Drive — API and browser

1. Open an isolated project with ended Auto-research and Experiment-loop
   episodes, an ended child Experiment, and an episode still running.
2. Inspect the **Started by** identity on project and space run rows. It is the
   recorded authorizer, including on the child, and remains the same when a
   different member views the project.
3. Archive an ended episode. It disappears from default project and space Runs
   and their section counts. Archiving a child also removes its nested link
   from its parent's Turns. Repeat with running and Needs Action episodes of
   both modes: Archive is available, their work continues, and archiving a
   parent does not archive or stop its child.
4. Reload and view the same project as another member. The archive remains
   effective for both members. Neither member's identity replaces the episode's
   original authorizer.
5. Enable **Show archived**. The episode appears in **Archived**, with its
   history still accessible. An archived older Experiment episode remains
   available after a newer episode starts and after the space's completed
   history window expires.
6. Unarchive the episode. It returns to its ordinary section with the same
   recorded lifecycle and attribution. Reports, task errors, retained stages,
   and canonical history are unchanged throughout.

## Assert

- The backend permits Archive in every episode lifecycle state while retaining
  ordinary project membership and write-admission checks.
- Shared archive state survives restart and project transfer, with attribution.
- Archive and Unarchive are idempotent and do not change execution or graph state.
- An archived episode stays archived through lifecycle changes until unarchived.
- Stale polling responses cannot restore an archived row in the browser.
- Archived rows never increase the ordinary Runs section counts.
- Missing legacy attribution never impersonates the viewer.
- The browser console, application requests, and server logs have no new errors.
