---
id: S108-repository-file-links-preserve-desktop-window
status: implemented
tier: hermetic
driver: desktop
covered_by:
  - tests/test_repository_preview.py
  - web/tests/repositoryFileLinks.test.mjs
  - web/tests/chatMarkdown.test.mjs
  - web/src-tauri/src/commands.rs
  - web/src-tauri/src/navigation.rs
  - web/src-tauri/src/windows.rs
  - desktop 2026-08-09 — live SSH preview, invalid-path error, and main-window reopen
invariants: [8]
last_passed: 2026-08-09 — a real path in the remote vista repository opened in
  the secondary native source preview, an unmatched scratch path stayed in the
  chat with an error, and closing then relaunching the main window restored the
  same project; local reads, ambiguity refusal, and bad-document recovery are
  covered by the named automated checks
---

# A repository file link never strands the desktop window

An agent answer may name a file in one of the project's repositories as a
Markdown link. Following that reference must not replace RCP's main webview with
an error document or leave the application running with no window the Dock can
restore.

## UI path — confirmed 2026-08-09

- In the desktop application, clicking a repository-file link in a rendered
  answer leaves the current RCP project, conversation, and scroll position in
  the main window. RCP resolves the target against the project's configured
  repositories rather than treating an absolute path as a URL on RCP's
  localhost origin.
- When the path resolves to exactly one configured repository, RCP opens an
  escaped read-only source page in the same secondary native preview window used
  for HTML artifacts. Local files are read locally; a file in an SSH repository
  is fetched on demand through that repository's configured SSH host. It is
  never reinterpreted as a desktop path, copied into the local repository, or
  retained in durable app storage.
- If the same absolute path falls under more than one configured repository
  root, including nested roots, RCP stays on the conversation and names every
  conflicting repository alias. It never prefers the longest root or guesses a
  host.
- If the reference is not a valid or openable repository file, RCP stays on the
  current screen and surfaces a concise error. It never navigates its main
  webview to a localhost error page as a fallback.
- Web references continue to open in the system browser. RCP's own application
  links continue to navigate within the main window.
- Closing the main window still hides it. Clicking RCP in the Dock always shows
  and focuses the existing main window. If the loaded document cannot answer
  RCP's normal prepare-to-show handshake, the native shell recovers to the
  verified RCP backend instead of leaving the window hidden.

The link does not grant an agent new filesystem authority: opening is a direct
human action, and the target is not read into the graph or chat by RCP.

## Drive

1. Open a project in the macOS desktop application and open a conversation
   whose answer contains a valid local repository-file link, a valid SSH-host
   repository-file link, and an invalid file link.
2. Click the local link. Confirm its escaped source opens in a secondary RCP
   preview window while the main window remains on the same conversation.
3. Click the SSH-host link. Confirm RCP reads the file from that host into its
   read-only source view and does not reinterpret the host path as local.
4. Click the invalid link. Confirm RCP reports the failure without leaving the
   conversation.
5. Exercise the recovery boundary by loading a non-RCP/error document in the
   main webview, close the window, and click the Dock icon.

## Assert

- `repository_file_link_never_navigates_the_main_webview`
- `valid_repository_file_link_uses_the_secondary_preview_window`
- `ssh_repository_file_link_reads_from_its_configured_host`
- `ssh_repository_file_link_never_opens_as_a_local_path`
- `ambiguous_repository_file_link_names_the_conflicting_aliases`
- `nested_repository_roots_are_ambiguous_not_preferred`
- `invalid_repository_file_link_is_visible_and_non_navigating`
- `web_reference_still_opens_in_the_system_browser`
- `dock_reopen_recovers_an_unresponsive_main_document`
- `recovered_window_is_visible_and_focused`

## Failure means

A repository reference destroys the current RCP surface, an invalid file path
silently does nothing, or the application remains alive in the Dock with no
window the human can reopen.
