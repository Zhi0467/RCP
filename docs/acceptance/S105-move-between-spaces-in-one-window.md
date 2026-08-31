---
id: S105-move-between-spaces-in-one-window
status: pending
tier: live
driver: desktop
covered_by: none
invariants: [8]
---

# One window, several spaces, no confusion about which one you are in

This scenario is human-confirmed and pending live qualification. Its boundaries
are in [Spaces and durable identity](../specs/projects-spaces-and-operations.md#spaces-and-durable-identity)
and [Confirmed team desktop target](../specs/api-web-and-desktop-projections.md#confirmed-team-desktop-target).

Every RCP backend serves its own interface. So selecting a team space points the
application window at that team server, and the screen a member is looking at is
always served by the backend answering it. That is what makes client/server skew
impossible for the application. A bootstrap/invitation code or existing token is
entered once through the controlled Add flow, cleared, and stored by the native
shell; later navigation exchanges the stored token for an ordinary session.

The index is the one screen showing more than one space at a time, so it stays
local.

This first client is the source-built RCP desktop app. It does not wait for a
Linux package or a packaged cross-platform release.

## Setup

A source-built RCP desktop app with a personal space holding at least one project, two
team-space invitations, and SSH access to two team spaces — one reachable, one
whose server can be stopped.

## Drive

1. Open the app cold, choose **Add team space**, enter the first SSH target,
   invitation code, and display name, and finish enrollment. Add the second
   connection with an existing permanent token.
2. Inspect both secret fields after submission, the credential store, and saved
   connection records. Restart the app without entering either token again.
3. Open a personal project. Work in it briefly.
4. Return to the index and open a project in the reachable team space.
5. Read the header. Inspect the window's origin, URL, page storage, saved
   connection record, logs, and native command output for the permanent token.
6. Return to the index and reopen the personal project.
7. Stop the second team space's server. Reload the index, then open a personal
   project and start a task.
8. Attempt to open a project in the stopped space.
9. Restart that server with a *different* space and reconnect the saved
   connection.
10. Point the app at a server whose `minimum_shell_version` exceeds the shell's.
11. Keep both team tunnels open, authenticate both spaces, and inspect their
    loopback origins and session cookies.
12. Remove the connection metadata while leaving its credential-store entry,
    then remove the credential while leaving metadata, and reconnect.

## Assert

- `the_index_lists_personal_and_team_projects_together`
- `the_index_renders_from_cached_cards_before_connections_reconcile`
- `add_team_space_enrolls_through_the_verified_tunnel`
- `the_one_secret_entry_is_cleared_and_never_persisted_as_page_state`
- `opening_a_team_project_navigates_to_that_servers_own_interface`
- `the_header_states_which_space_is_active`
- `the_permanent_token_exists_only_in_the_operating_system_credential_store`
- `the_permanent_token_is_absent_from_page_storage_urls_logs_and_command_output`
- `the_session_is_established_before_navigation_and_is_http_only`
- `every_space_uses_a_stable_distinct_loopback_origin_not_just_a_different_port`
- `two_team_spaces_cannot_overwrite_or_receive_each_others_session_cookie`
- `personal_work_continues_while_a_team_connection_is_unavailable`
- `an_unavailable_team_space_is_reported_and_never_silently_rerouted_locally`
- `the_local_backend_never_executes_or_applies_team_work`
- `an_unexpected_space_id_blocks_mutations_until_the_human_reconnects`
- `a_shell_below_the_minimum_version_is_told_to_update`
- `connection_metadata_and_credential_lifecycle_reconcile_without_exposing_the_token`

## UI path

The project index groups projects by space: the personal space first, then each
saved team connection with its name and reachability. Pending project
invitations appear here as cards ([S122](S122-project-invitations.md)).

Opening a team project reloads the window into that server's interface. The
project shell's header names the active space. Returning to the index reloads
back to the local one.

An unreachable team connection appears as a named, dimmed group with its last
known cards, not as an error that blocks the page.

Switching origins shows one compact **Connecting to <space>** state until the
handshake either succeeds or produces a specific retry/reconnect action. It adds
no second app shell and no stream of transport commentary.

Deliberately not possible: acting in a team project while its connection is
down, and any indication that a team project can be worked on locally.

## Boundary

Switching spaces is a page load, not a tab switch. Cross-space application state
is lost by design; remembered view state is project-scoped and returns when the
project does.

This scenario is `driver: desktop` because the promise is about the application
window — its origin, credential store, cookie jar, SSH tunnel, and navigation
between backends. Browser tooling does not prove those native boundaries. Drive
it through the source-built application, accessibility tree, screenshots, shell
logs, and inspected credential/connection stores.

The connection handshake is an API projection contract; this scenario asserts
only what the window does with it.

## Implemented substrate

D2 through D5 are implemented as of 2026-08-30. The production desktop allocates
a stable connection-bound HTTPS localhost alias, keeps its bounded identity in
an authenticated encrypted file with the sealing key in Keychain, installs its
exact certificate pin on the live main WKWebView, and rejects navigation outside
the validated saved-origin set. The retained two-origin
WKWebView drive passes login and restart phases, including cookie isolation and
an unpinned-certificate refusal. The saved-connection owner also launches and
reuses the direct system-SSH forward, wraps it in that TLS identity, and reaps
the exact local process group on stop. Admission prevents launch after removal,
Quit, or update cleanup; failed cleanup remains owned, and a team origin cannot
launch another saved connection. A live authenticated drive passed through the
production manager without changing the remote host. The native enrollment,
session exchange, cookie installation, navigation, grouped index, cached
unavailable cards, and reconnect paths now have focused Rust/Web coverage. This
scenario remains pending because those D4-D5 paths have not yet been driven
together through the real source-built app against the two live team spaces.
