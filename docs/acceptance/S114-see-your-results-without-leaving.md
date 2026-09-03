---
id: S114-see-your-results-without-leaving
status: pending
tier: live
driver: pytest + browser + desktop + ssh
covered_by:
  - tests/test_unified_artifacts.py
  - tests/test_api.py::test_chat_artifacts_are_bounded_sandboxed_and_independent
  - tests/test_result_view_artifacts.py
  - web/tests/resultViews.test.mjs
invariants: [1, 2, 4, 6, 10e]
last_checked: 2026-09-02 — the source-generated viewer was driven in Safari's
  WebKit: highlighting text enabled Add highlighted text without creating a rail
  item, and the explicit button created exactly one commentable selection. A
  source-built desktop also opened and downloaded the live TIDMAD artifact over
  the WTH UCSD team's SSH-tunneled HTTPS origin, then closed the preview without
  leaving the team project. The earlier browser drive covers Box, Add to chat,
  Keep, and live external reread; remote Keep remains pending.
---

# Ask about any task artifact without leaving its chat

Confirmed by the human 2026-08-27. A so-called result view is an ordinary task
artifact, not an Experiment-only record or code path. Any chat may produce one.
Small raster and SVG artifacts render inline; HTML keeps its ordinary Open link.
All supported artifacts and episode reports open in one viewer shell.

The viewer's text and box selections are transient prompt context, not saved
annotations. The human comments on selections, reviews the assembled chat draft,
and sends it. The originating native session answers every comment and question.
It edits the artifact only when the human explicitly asks and sends Work.

## Setup

A project with one Node chat, one Project chat, a completed episode report, and
ordinary task artifacts consisting of a small PNG, an SVG, and an interactive
HTML result page. The state repository already contains an `artifacts/`
directory with unrelated human files. Repeat the storage drive with remote
canonical state.

## Drive

1. Produce the three task artifacts from the Node chat. Confirm the PNG and SVG
   appear inline with the answer and the HTML appears as an Open link.
2. Open the HTML. Confirm the unified viewer shows the page, transient-selection
   rail, and Keep control; no result-view destination, selector, or second card
   exists.
3. Highlight text and confirm it remains an ordinary selection until the human
   explicitly adds it to the rail. Add that highlight, then box a region. Add
   separate comments and add the assembled context to the chat. Confirm no task
   starts automatically and the editable composer remains in Discuss.
4. Send a question. Confirm the exact originating native session receives the
   bounded selections, comments, final question, and a read-only current copy
   of the artifact. Confirm it answers without editing the file.
5. Make another selection whose comment explicitly asks for a visual change,
   choose Work, and send. Confirm the same session updates the original file and
   card in place, with no second artifact.
6. Keep the artifact. Confirm it lands under repository-root `artifacts/`
   without changing or overwriting the existing human files.
7. Edit the kept file outside RCP. Reopen it and ask a question; confirm the
   viewer and resumed session see the external edit without a digest conflict.
8. Explicitly request another Work edit. Confirm the same kept file changes in
   place and remains revisable.
9. Repeat the selection flow for the PNG, SVG, and episode report. Confirm each
   uses the same viewer shell and the report remains immutable.
10. With a retained desktop client that still opens the former `/preview` URL,
    pull the updated source and restart RCP. Open the report and task artifact;
    confirm both old URLs enter the same shell without rebuilding the client,
    while its old inline PNG and SVG requests still render as images.
11. Open the Project chat and confirm only that chat's artifacts appear there.
12. Make the source native session unavailable. Confirm the send fails visibly
    and offers an explicit fresh-session path without silently taking it.
13. Repeat Keep and live reread with a remote state repository.

## Assert

- `there_is_one_task_artifact_concept_and_no_new_result_view_path`
- `artifacts_are_visible_only_in_their_originating_chat`
- `small_raster_and_svg_artifacts_render_inline`
- `html_keeps_open_link_behavior_without_an_inline_thumbnail`
- `task_artifacts_and_episode_reports_use_one_viewer_shell`
- `a_source_update_upgrades_legacy_desktop_preview_urls_to_the_viewer_shell`
- `legacy_inline_image_requests_remain_images_after_the_source_update`
- `text_and_box_selections_are_transient_not_persisted_annotations`
- `highlighting_text_does_not_capture_it_without_an_explicit_action`
- `box_context_includes_bounded_coordinates_and_visible_labels`
- `selection_comments_assemble_into_a_visible_editable_chat_draft`
- `adding_context_never_dispatches_a_turn`
- `selection_keeps_discuss_as_the_default_mode`
- `the_prompt_addresses_comments_and_questions_without_implying_an_edit`
- `editing_requires_an_explicit_human_request_and_work_mode`
- `the_exact_originating_native_session_receives_the_turn`
- `unavailable_session_fails_visibly_and_never_silently_falls_back`
- `a_revision_updates_the_same_file_identity_and_card`
- `keep_uses_repository_root_artifacts_outside_dot_research`
- `an_existing_real_artifacts_directory_and_its_files_are_preserved`
- `an_unsafe_artifacts_entry_makes_keep_fail_visibly`
- `initial_keep_never_overwrites_a_name_collision`
- `kept_artifacts_remain_live_and_revisable`
- `external_edits_are_read_normally_without_a_digest_precondition`
- `keeping_or_revising_an_artifact_grants_no_graph_authority`
- `remote_keep_and_live_reread_follow_the_same_contract`

## Failure means

RCP still has a special result-view selector, record, or Experiment-only UI;
selection sends immediately, silently forces Work, or always asks the agent to
edit; revision creates a second artifact; Keep freezes bytes, writes under
`.research/`, overwrites an existing file, or rejects ordinary external edits.
