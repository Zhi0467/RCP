---
id: S117-project-owned-caches
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_cache_lifecycle.py
  - tests/test_project_deletion.py
  - tests/test_api.py
  - web/tests/api.test.mjs
  - web/tests/projectCacheSettings.test.mjs
  - browser 2026-08-12
invariants: [1, 2]
last_passed: 2026-08-12
---

# Clear one project's cache without clearing another project's cache

**Confirmed by the human 2026-08-12:** the ordinary cache meter and Clear action
in project Settings belong to that project. Clearing every project's caches is
a separate app-wide danger action with an explicit warning and confirmation.

Both actions affect only rebuildable remote-source copies and derived session
slices. Neither action deletes provider originals, canonical `.research/`
state, task history, chats, paper drafts, result views, or repository files.

## Setup

Open two projects with distinct cached remote-source files and session slices.
Keep one active cache reader in each project in separate runs. Include one
legacy entry from the former shared app-wide cache layout.

## Drive

1. Open project A → **Settings → Project cache**. Its meter counts only project A's
   entries; project B's entries do not appear.
2. With no active project-A task, press **Clear project cache**. No destructive
   confirmation is required because every target is project-A-owned and
   rebuildable.
3. Confirm project A reaches zero while project B and the legacy shared entry
   remain byte-identical.
4. Run a task in project A and confirm project A's clear is unavailable. Run a
   task only in project B and confirm project A can still clear its own cache.
5. Press the separate **Clear all project caches** danger action.
6. Read a warning that this clears rebuildable caches for every project, then
   cancel and confirm nothing changed.
7. Confirm the warning. The app clears project A, project B, and safely
   identifiable legacy shared cache entries.
8. Repeat while any project has an active task and confirm the app-wide action
   is refused without deleting anything.

## Assert

- `cache_roots_and_metrics_are_project_owned`
- `project_clear_removes_only_that_projects_rebuildable_entries`
- `another_projects_active_task_does_not_block_project_clear`
- `the_same_projects_active_task_blocks_project_clear`
- `app_wide_clear_is_a_separate_danger_action`
- `app_wide_clear_requires_an_explicit_warning_confirmation`
- `cancelling_app_wide_clear_changes_nothing`
- `app_wide_clear_removes_every_inactive_project_cache_and_legacy_shared_cache`
- `any_active_task_blocks_app_wide_clear`
- `canonical_state_original_sources_and_operational_records_are_unchanged`
- `cache_actions_have_no_console_or_request_errors`

## UI path

Project Settings labels the ordinary section **Project cache** and its normal
button **Clear project cache**. The meter names remote sources and session
slices for the open project only.

A visually separate danger action is labelled **Clear all project caches**.
Selecting it opens an alert dialog:

> Clear caches for every project? Rebuildable remote-source copies and session
> slices for all projects will be removed. Canonical research and original
> provider data are not affected.

The final destructive button repeats **Clear all project caches**. This warning
is required UI, not helper copy under the ordinary project action.
