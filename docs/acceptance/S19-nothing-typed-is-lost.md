---
id: S19-nothing-typed-is-lost
status: implemented
tier: hermetic
driver: browser
covered_by: none
invariants: [3]
reported_by: human, 2026-07-30
last_passed: 2026-07-30 — agent-driven against a throwaway copy of the demo project
---

# Nothing typed is ever lost

Every surface where a human types holds what was typed until the human commits
it or discards it. This is a floor, not a feature. The graph draft and Settings
now both meet it.

## The defect this came from — fixed

Reported: typing in Settings, moving to another panel, edits gone. The original
implementation had three loss paths:

1. **Unmount.** Component-local state disappeared when the person navigated
   away.
2. **Background refresh.** The form reset whenever the `project` object identity
   changed, even when its settings values were identical.
3. **Self-inflicted.** Clearing caches produced another project snapshot and
   triggered the same reset.

[ProjectSettings.tsx](../../web/src/views/ProjectSettings.tsx) now restores a
project-keyed local draft, stages every edit, and reinitializes the form only
when the project id changes. Save and Reset deliberately clear that staged
copy.

There is no conflict case to design for. One process owns a data directory
(invariant 8) and agents cannot write the manifest (invariant 4), so the only
writer of these fields is the human in this UI.

## Two stores, two verbs, one floor

Settings and the graph draft are correctly separate systems. They should stay
separate, and they should share the floor:

| | Stages locally | Commits via | Goes to |
|---|---|---|---|
| Graph edits | yes, already | **Sync** | patch log |
| Settings | yes | **Save** | manifest |

**Sync means commit to canonical history**, not push to a remote. It is the
human authority boundary (invariant 3); canonical living on another machine is
incidental (invariant 6), and a fully local project still needs the boundary.
Settings reports **Saved**, keeping the manifest write distinct from graph
Sync.

## Drive

Settings, three ways:

1. Edit the default run scope and an agent profile. Navigate to another view.
   Come back.
2. Edit again. While the edits are unsaved, let a background agent run complete
   successfully.
3. Edit again. Clear caches from within Settings.
4. Edit again. Reload the browser.
5. Edit, then Save. Then edit and press Reset.

Graph draft, the same shape — expected to pass already:

6. Edit a node's prose, set a standing, approve a proposal. Navigate away, come
   back, reload the browser.
7. Sync. Then confirm the draft is cleared.

## Assert

- `settings_edits_survive_navigation`
- `settings_edits_survive_a_background_run` — the form is **not** reset when a
  snapshot refresh carries identical values
- `settings_edits_survive_clearing_caches`
- `settings_edits_survive_reload`
- `switching_projects_does_reset_the_form` — the one case where a reset is
  correct
- `save_clears_the_staged_copy`
- `reset_clears_the_staged_copy` — explicit discard is the other correct way to
  lose an edit
- `save_status_does_not_say_synced`
- `graph_draft_survives_navigation_and_reload` — already true
- `sync_clears_the_graph_draft` — already true

## Failure means

The human typed something and the app threw it away. Everything else in RCP is
built on the human being the authority; silently discarding what they wrote is
the most direct possible contradiction of that.
