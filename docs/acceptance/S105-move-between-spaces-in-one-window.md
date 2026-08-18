---
id: S105-move-between-spaces-in-one-window
status: pending — not human-confirmed
tier: packaged
driver: desktop
covered_by: none
invariants: [8]
---

# One window, several spaces, no confusion about which one you are in

This scenario is a proposal and is **not yet human-confirmed**. The current
boundaries are in [Spaces and durable identity](../specs/projects-spaces-and-operations.md#spaces-and-durable-identity)
and [Project index and identity](../specs/api-web-and-desktop-projections.md#project-index-and-identity).

Every RCP backend serves its own interface. So selecting a team space points the
application window at that team server, and the screen a member is looking at is
always served by the backend answering it. That is what makes client/server skew
impossible for the application, and it is why the credential never has to reach
a page: the desktop shell presents the token once and exchanges it for an
ordinary session before navigating.

The index is the one screen showing more than one space at a time, so it stays
local.

## Setup

A packaged desktop build with a personal space holding at least one project, and
saved connections to two team spaces — one reachable, one whose server can be
stopped.

## Drive — proposal

1. Open the app cold and read the project index.
2. Open a personal project. Work in it briefly.
3. Return to the index and open a project in the reachable team space.
4. Read the header. Inspect the window's origin and its storage for the
   permanent token.
5. Return to the index and reopen the personal project.
6. Stop the second team space's server. Reload the index, then open a personal
   project and start a task.
7. Attempt to open a project in the stopped space.
8. Restart that server with a *different* space and reconnect the saved
   connection.
9. Point the app at a server whose `minimum_shell_version` exceeds the shell's.

## Assert

- `the_index_lists_personal_and_team_projects_together`
- `the_index_renders_from_cached_cards_before_connections_reconcile`
- `opening_a_team_project_navigates_to_that_servers_own_interface`
- `the_header_states_which_space_is_active`
- `the_permanent_token_is_absent_from_page_storage_and_from_the_url`
- `the_session_is_established_before_navigation_and_is_http_only`
- `personal_work_continues_while_a_team_connection_is_unavailable`
- `an_unavailable_team_space_is_reported_and_never_silently_rerouted_locally`
- `the_local_backend_never_executes_or_applies_team_work`
- `an_unexpected_space_id_blocks_mutations_until_the_human_reconnects`
- `a_shell_below_the_minimum_version_is_told_to_update`

## UI path (proposal)

The project index groups projects by space: the personal space first, then each
saved team connection with its name and reachability. Pending project
invitations appear here as cards ([S122](S122-project-invitations.md)).

Opening a team project reloads the window into that server's interface. The
project shell's header names the active space. Returning to the index reloads
back to the local one.

An unreachable team connection appears as a named, dimmed group with its last
known cards, not as an error that blocks the page.

Deliberately not possible: acting in a team project while its connection is
down, and any indication that a team project can be worked on locally.

Open for a human answer: whether a reload between spaces needs any transition
treatment, or whether it should be as bare as it sounds.

## Boundary

Switching spaces is a page load, not a tab switch. Cross-space application state
is lost by design; remembered view state is project-scoped and returns when the
project does.

This scenario is `driver: desktop` because the promise is about the application
window — its origin, its cookie jar, and its navigation between backends. None
of it is reachable through browser tooling, and no automated desktop harness
exists yet, so it is driven manually through the built application, its
accessibility tree, screenshots, and shell logs.

The connection handshake is an API projection contract; this scenario asserts
only what the window does with it.
