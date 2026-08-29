---
id: S26-delete-project
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_project_deletion.py
  - tests/test_project_delete_api.py
  - tests/test_team_project_deletion_guard.py
  - web/tests/landingIdentity.test.mjs
  - browser 2026-07-31 — personal deletion flow
  - browser 2026-08-29 — team action omission
last_passed: >-
  2026-08-29 — personal deletion remains green, while team cards, direct API,
  catalog, and rendered action-menu guards refuse ordinary deletion before any
  project or managed-machine state changes.
invariants: [1, 2, 8]
---

# Delete a personal RCP project without deleting the research project

Deleting a personal project removes it from RCP and erases the app-owned records
that belong only to that registration. It never deletes or edits the repository,
canonical `.research/` state, append-only patches, provider conversation logs,
or any other source material. This implemented scenario predates team projects
and remains the personal-space contract.

## UI path

Each project cover on the project index has one compact action menu. The menu
keeps the existing cover choice and adds **Delete project** as a destructive
action. Selecting it opens a confirmation naming the project and stating the
exact boundary: RCP records will be erased; repositories and `.research/` will
not be touched. The final destructive button is also labeled **Delete project**.

A queued, running, or pausing task blocks deletion. The confirmation directs
the human to pause it first, and the API independently refuses the deletion.
A paused, interrupted, failed, or completed task does not block deletion, but
the confirmation states that its resumable stage and RCP history will no longer
be reachable through the app.

On success, RCP returns to the project index and the cover is gone. A stale
deep link reports that the project no longer exists rather than re-registering
it from a cached snapshot.

## Drive

1. Register a temporary project, open it once, and create task history, a local
   paper draft, a writing session, and a display snapshot.
2. Return to the project index, open the project's action menu, and choose
   **Delete project**.
3. Cancel once and confirm that nothing changed.
4. Start an agent task and confirm deletion is refused while it is active.
5. Pause the task, confirm deletion, and restart RCP.
6. Inspect the repository and canonical `.research/` state.

## Assert

- `delete_requires_confirmation`
- `cancel_preserves_everything`
- `active_task_blocks_delete`
- `project_disappears_immediately_and_after_restart`
- `stale_project_link_returns_not_found`
- `project_database_records_are_removed`
- `project_display_snapshot_is_removed`
- `repository_and_research_history_are_byte_identical`
- `no_console_or_application_request_errors`

## Failure means

The project remains stuck in the index, an agent process outlives the records
that own it, deleted app-only data silently returns, or RCP touches canonical
research history while performing an app-catalog operation.

## Team boundary

Ordinary deletion is not team-project deprovisioning. The backend publishes team
deletion unavailable, the Web omits the action, and the API plus catalog reject
a direct attempt before touching either RCP records or managed machine state. A
future operator-owned deprovision flow must separately decide checkout
disposition and Git deploy-key revocation.
