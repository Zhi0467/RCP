---
id: S26-delete-project
status: pending
tier: live
driver: pytest + browser
covered_by:
  - tests/test_project_deletion.py
  - tests/test_project_delete_api.py
  - tests/test_team_project_deletion.py
  - web/tests/landingIdentity.test.mjs
  - browser 2026-07-31 — personal deletion flow
  - browser 2026-08-29 — former team action omission
last_passed: >-
  2026-08-29 — personal deletion remains live-verified. Team deletion now has
  hermetic API, catalog, restart, and rendered-action coverage, while its
  source-built desktop drive against a disposable transferred project remains.
invariants: [1, 2, 8]
---

# Delete an RCP project without deleting the research project

Deleting a personal or team project removes it from that RCP space and erases
the app-owned records that belong only to the registration. It never deletes or
edits the repository, canonical `.research/` state, append-only patches, or any
other checkout material. For a team project it also leaves the repository
deploy key in place. That is intentionally catalog deletion, not machine
deprovisioning.

## UI path

Each project cover on the project index has one compact action menu. The menu
keeps the existing cover choice and adds **Delete project** as a destructive
action. Selecting it opens a confirmation naming the project and stating the
exact boundary: RCP records will be erased; repositories and `.research/` will
not be touched. The final destructive button is also labeled **Delete project**.

A queued, running, or pausing task blocks deletion in either space. The confirmation directs
the human to pause it first, and the API independently refuses the deletion.
A paused, interrupted, failed, or completed task does not block deletion, but
the confirmation states that its resumable stage and RCP history will no longer
be reachable through the app.

On success, RCP returns to the project index and the cover is gone. A stale
deep link reports that the project no longer exists rather than re-registering
it from a cached snapshot.

## Drive

1. Register a temporary personal project, open it once, and create task history,
   a local paper draft, a writing session, and a display snapshot.
2. Return to the project index, open the project's action menu, and choose
   **Delete project**.
3. Cancel once and confirm that nothing changed.
4. Start an agent task and confirm deletion is refused while it is active.
5. Pause the task, confirm deletion, and restart RCP.
6. Inspect the repository and canonical `.research/` state.
7. Through the source-built desktop, move a disposable personal project into a
   team space. Record the managed checkout, canonical history, deploy key, RCP
   rows, imported provider history, snapshots, caches, and stopped task stage.
8. Delete it from the team project card, restart both app and server, and inspect
   every recorded boundary.

## Assert

- `delete_requires_confirmation`
- `cancel_preserves_everything`
- `active_task_blocks_delete`
- `project_disappears_immediately_and_after_restart`
- `stale_project_link_returns_not_found`
- `project_database_records_are_removed`
- `project_display_snapshot_is_removed`
- `repository_and_research_history_are_byte_identical`
- `team_project_delete_uses_the_same_confirmed_action`
- `team_project_disappears_immediately_and_after_server_restart`
- `team_project_database_stages_snapshots_caches_and_imported_history_are_removed`
- `team_managed_checkout_research_history_and_deploy_key_are_byte_identical`
- `no_console_or_application_request_errors`

## Failure means

The project remains stuck in the index, an agent process outlives the records
that own it, deleted app-only data silently returns, or RCP touches canonical
research history while performing an app-catalog operation.

## Team boundary

Ordinary team deletion is not deprovisioning. It deliberately leaves the
server-managed checkout and deploy key untouched, just as personal deletion
leaves a person's checkout untouched. Removing the checkout or revoking the key
would be a separate machine/operator action and is not implied by the project
card's **Delete project** confirmation.
