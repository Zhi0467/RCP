---
id: S126-choose-local-repository-folder
status: implemented
tier: hermetic
driver: web + desktop
covered_by:
  - web/tests/desktopRuntime.test.mjs
  - web/tests/projectSetup.test.mjs
  - web/src-tauri/src/commands.rs::tests::folder_selection_result_preserves_cancel_and_path
reported_by: confirmed by the human on 2026-08-24
---

# Choose a local repository folder in the desktop setup wizard

The desktop project-setup wizard lets a researcher choose each local repository
through the native macOS folder picker instead of requiring a manually copied
absolute path. The browser and SSH setup paths remain truthful about why they
still require text entry.

## Drive

1. Open **New project** in the desktop app with a local repository selected.
2. Choose **Choose folder…**, select an existing folder in Finder, and confirm.
3. Confirm that the native dialog remains responsive and that the repository
   path becomes the selected absolute path.
4. Open the picker again and cancel. Confirm that the existing path remains.
5. Switch the repository to SSH. Confirm that the Finder action disappears and
   the remote absolute path remains a text field.
6. Open the same wizard in a browser. Confirm that local setup explains that an
   absolute path must be pasted and that Finder selection is desktop-only.

## Assert

- `desktop_local_repository_uses_native_folder_picker`
- `selected_folder_fills_the_absolute_path`
- `native_folder_dialog_does_not_block_the_desktop_event_loop`
- `cancel_preserves_the_existing_path`
- `ssh_repository_never_offers_a_local_folder_picker`
- `browser_local_repository_explains_manual_path_entry`
- `every_repository_editor_uses_the_same_behavior`

## Boundary

Folder selection grants no new repository authority and does not create,
register, preflight, or mutate a project. The existing setup confirmation and
backend path checks remain the authority boundary.
