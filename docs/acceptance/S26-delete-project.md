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
  - source desktop 2026-09-04 — disposable transferred team project deletion
last_passed: >-
  2026-08-29 — personal deletion remains live-verified. Team deletion has
  hermetic API, catalog, restart, and rendered-action coverage.
last_checked: >-
  2026-09-04 — transferred team-project confirmation, Cancel, deletion,
  direct project-row removal, preserved checkout/canonical bytes/deploy key,
  and absence after desktop restart passed. Production-server restart and
  nonempty task-stage/imported-history cleanup remain unqualified live.
invariants: [1, 2, 8, 9, 10g]
---

# Delete an RCP project without deleting the research project

The 2026-09-04 source desktop deleted disposable transferred project
`c3997083-a534-47f6-a4c1-74286f93422c` after Cancel first preserved it. The
dialog named checkout and deploy-key preservation. All 11 target `.research`
file hashes and Git HEAD were unchanged; GitHub key 162317885 remained present
and writable. A read-only inspection found no direct project-owned rows in
21 tables, SQLite integrity `ok`, and no foreign-key violations. The card stayed
absent after desktop restart and fresh team connection. The production service
was deliberately not restarted. This small fixture does not prove the broader
active-work refusal or nonempty app-file cleanup cases below; status stays
pending. Exact receipts are in the
[production qualification handoff](../handoffs/handoff-2026-08-27-dev-team-space-and-server.md#production-qualification-receipts-and-next-boundary--2026-09-04).

Deleting a personal or team project removes it from that RCP space and erases
the app-owned records that belong only to the registration. It never deletes or
edits the repository, canonical `.research/` state, append-only patches, or any
other checkout material. For a team project it also leaves the repository
deploy key in place. That is intentionally catalog deletion, not machine
deprovisioning. App-owned file cleanup follows the database commit; a cleanup
failure is warned and may leave inert files without making deletion fail.

## UI path

Each project cover on the project index has one compact action menu. The menu
keeps the existing cover choice and adds **Delete project** as a destructive
action. Selecting it opens a confirmation naming the project and rendering the
backend consequence text. Both spaces say RCP records are erased while
repositories and `.research` remain untouched. A team project additionally
says: **The server-managed checkout and repository deploy key remain;
credentials are not revoked.** The final destructive button is also labeled
**Delete project**.

A queued, running, or pausing task; any non-terminal episode; any unfinished
project transfer; any active watcher; and any degraded or completed watcher with
an undelivered notification blocks deletion in either space. The API directs
the human to Pause the task, use episode Stop, or stop watching, then refuses the
deletion. Settled work does not block deletion, but the confirmation states that
its resumable stage and RCP history will no longer be reachable through the app.

Before changing SQLite or files, RCP validates all file cleanup boundaries,
including the complete imported-source project root. The database transaction
repeats the active-work check and removes all project-owned rows before any file
is removed. Post-commit file failures are WARNING diagnostics naming the project,
path, and reason; the missing registration remains the successful outcome.

On success, RCP returns to the project index and the cover is gone. A stale
deep link reports that the project no longer exists rather than re-registering
it from a cached snapshot.

## Drive

1. Register a temporary personal project, open it once, and create task history,
   a local paper draft, a writing session, and a display snapshot.
2. Return to the project index, open the project's action menu, and choose
   **Delete project**.
3. Cancel once and confirm that nothing changed.
4. Confirm deletion is refused separately for an active task, a live episode,
   and an active or deliverable watcher. Settle each through its existing control.
5. Confirm deletion and restart RCP.
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
- `live_episode_blocks_delete_until_stop_settles`
- `active_or_deliverable_watcher_blocks_delete_until_stop_watching`
- `failed_filesystem_preflight_preserves_registration_stages_snapshots_and_caches`
- `project_disappears_immediately_and_after_restart`
- `stale_project_link_returns_not_found`
- `project_database_records_are_removed`
- `project_display_snapshot_is_removed`
- `repository_and_research_history_are_byte_identical`
- `team_project_delete_uses_the_same_confirmed_action`
- `team_project_disappears_immediately_and_after_server_restart`
- `team_project_owned_database_rows_are_removed_in_one_transaction`
- `normal_team_file_cleanup_removes_stages_snapshots_caches_and_imported_history`
- `post_commit_file_cleanup_failure_warns_without_resurrecting_registration`
- `deleted_project_invitation_cannot_be_accepted`
- `team_confirmation_names_checkout_deploy_key_and_non_revocation`
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
