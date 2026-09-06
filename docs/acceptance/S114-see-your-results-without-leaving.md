---
id: S114-see-your-results-without-leaving
status: pending
tier: live
driver: pytest + browser + desktop + ssh
covered_by:
  - tests/test_unified_artifacts.py
  - tests/test_api.py::test_chat_artifacts_are_bounded_sandboxed_and_independent
  - tests/test_result_view_artifacts.py
  - tests/test_episode_api.py::test_episode_report_preview_is_singular_and_sandboxed
  - tests/test_episode_api.py::test_save_episode_report_copies_immutable_bytes_without_overwriting
  - tests/test_episode_api.py::test_save_episode_report_failure_is_visible_and_retryable
  - tests/test_episode_api.py::test_save_episode_report_rejects_missing_report
  - web/tests/resultViews.test.mjs
  - web/tests/artifactSelection.browser.test.mjs
invariants: [1, 2, 4, 6, 9, 10e]
last_checked: 2026-09-06 — Save copy passed in Chromium and WebKit against a live
  local server with synthetic report data and no originating chat. The displayed
  artifacts/ path resolved to the exact captured HTML. An existing file blocking
  artifacts/ produced a visible error; removing the obstruction allowed retry.
  API regressions verify collision preservation and unchanged report, episode,
  and graph state. Remote publication and native desktop save remain unverified;
  this scenario stays pending. On 2026-09-05, direct selection and confirmation
  passed in Chromium and WebKit using the source-generated nested viewer with
  synthetic report data.
  Text stays highlighted; area drags need no Box mode; neither enters the rail
  before Comment. Cancel, Escape, repeated drags and report controls were checked.
  Earlier production verification used a source-built desktop to open and
  download the live TIDMAD artifact over
  the WTH UCSD team's SSH-tunneled HTTPS origin, then closed the preview without
  leaving the team project. Candidate-disposition regressions pass; the browser
  drive covers Box, comment, Add to chat, Keep, and live external reread. A
  rebuilt RCP Dev restart and exact Open report click show the shell with
  `/content` nested, and the legacy `/preview` URL returns that shell. Remote
  Keep and SSH remain pending.
---

# Ask about any task artifact without leaving its chat

Confirmed by the human 2026-08-27. A so-called result view is an ordinary task
artifact, not an Experiment-only record or code path. Any chat may produce one.
Small raster and SVG artifacts render inline; HTML keeps its ordinary Open link.
All supported artifacts and episode reports open in one viewer shell.

The human requested the missing episode-report save control on 2026-09-06 and
authorized its PR after the repository-copy behavior was described. **Save copy**
publishes captured report HTML into the state repository's `artifacts/` directory
and shows its repository-relative path. It preserves the immutable stored report
and remains available without an originating chat.

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
3. Highlight text, then choose Comment to add it to the rail. Drag a figure or
   blank area directly, without a Box button. Confirm the rectangle remains
   visible until Comment, Cancel or Escape; neither a highlight nor an area
   enters the rail automatically. Confirm ordinary report controls still work. Add
   separate comments and add the assembled context to the chat. Confirm no task
   starts automatically and the editable composer remains in Discuss.
4. Send a question. Confirm the exact originating native session receives the
   bounded selections, comments, final question, and a read-only current copy
   of the artifact. Confirm it answers without editing the file.
5. Make another selection whose comment explicitly asks for a visual change,
   choose Work, and send. Confirm the same session produces one pending candidate,
   leaves the original unchanged, and adds no second artifact card. Compare
   Current and Candidate, Reject once, and confirm the original remains unchanged.
6. Keep the artifact. Confirm it lands under repository-root `artifacts/`
   without changing or overwriting the existing human files.
7. Edit the kept file outside RCP. Reopen it and ask a question; confirm the
   viewer and resumed session see the external edit normally.
8. Explicitly request another Work edit. Confirm the original remains current
   until Accept, then Accept and confirm the same kept file changes in place and
   remains revisable. Retry Accept and confirm it is idempotent.
9. Repeat the selection flow for the PNG, SVG, and episode report. Confirm each
   uses the same viewer shell and the report remains immutable.
10. With a retained desktop client that still opens the former `/preview` URL,
    pull the updated source and restart RCP. Open the report and task artifact;
    confirm both old URLs enter the same shell without rebuilding the client,
    while its old inline PNG and SVG requests still render as images.
11. Open the Project chat and confirm only that chat's artifacts appear there.
12. Make the source native session unavailable. Confirm the send fails visibly
    and offers an explicit fresh-session path without silently taking it.
13. Produce a candidate, edit the source externally, then Accept. Confirm RCP
    reports Conflict without overwriting either version; Reject the candidate.
14. Interrupt Accept after candidate publication but before its decision row is
    finalized. Retry and confirm recovery recognizes the already-published digest.
15. Keep a temporary source while its candidate is pending, then Accept. Confirm
    the kept location is resolved at disposition time. Repeat temporary, kept,
    candidate, Accept, Reject, and interrupted-Accept paths with remote stages.
16. Confirm unresolved candidates protect their exact stages from cleanup and
    block project transfer; disposition releases both constraints.
17. Carry a pending local candidate through a server update checkpoint. Then
    restore an offline backup containing another pending candidate and confirm
    it becomes Abandoned while its original remains unchanged.
18. Force one Work graph-correction turn after it first writes the replacement.
    Confirm only the final corrected bytes become the candidate and no extra or
    wrong-name artifact card appears. Inspect the provider launch receipt and
    confirm the source directory is in its protected-write set.
19. Open an episode report and choose **Save copy**. Resolve the displayed
    `artifacts/` path relative to the state repository and compare the file bytes
    with the captured report HTML. Confirm the stored report, its sandboxed
    preview, the episode, and graph history remain unchanged. Repeat with no
    originating chat and through the retained desktop `/preview` entrance.
20. Edit the saved copy externally, then save again. Confirm the new path is
    distinct, the new file contains the original captured report, and both the
    human edit and unrelated files remain unchanged. Reload the report viewer
    and confirm it still shows the captured report rather than the edited copy.
21. In disposable test data, make `artifacts/` a file or symlink. Save and confirm
    a visible error, an enabled retry control, and no alteration of the existing
    entry. Remove the obstruction and retry; confirm the displayed path resolves
    to the successful copy. A missing report must refuse save without creating
    an artifact.
22. Repeat the report-save steps with a reachable remote canonical-state host
    and a disposable state repository. Verify bytes and collision preservation
    on that host, not in a local cache. Interrupt SSH access before saving;
    confirm visible failure with the stored report intact, restore access, and
    retry. Inspect browser errors, network responses, and server logs.

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
- `work_creates_one_pending_candidate_without_mutating_the_source`
- `current_and_candidate_are_compared_before_explicit_human_disposition`
- `accept_updates_the_same_file_identity_and_card`
- `reject_keeps_the_original_unchanged`
- `an_intervening_edit_conflicts_instead_of_being_overwritten`
- `interrupted_accept_recovers_from_the_candidate_digest`
- `only_one_unresolved_candidate_exists_per_source_artifact`
- `unresolved_candidates_protect_their_stage_and_block_transfer`
- `update_checkpoint_preserves_but_offline_restore_abandons_pending_candidates`
- `candidate_capture_happens_after_corrections_with_source_write_denied`
- `keep_uses_repository_root_artifacts_outside_dot_research`
- `an_existing_real_artifacts_directory_and_its_files_are_preserved`
- `an_unsafe_artifacts_entry_makes_keep_fail_visibly`
- `initial_keep_never_overwrites_a_name_collision`
- `kept_artifacts_remain_live_and_revisable`
- `external_edits_are_read_normally_until_an_accept_digest_precondition`
- `keeping_or_revising_an_artifact_grants_no_graph_authority`
- `remote_keep_and_live_reread_follow_the_same_contract`
- `episode_report_save_copy_works_without_an_originating_chat`
- `report_save_shows_the_state_repository_relative_path_to_exact_captured_bytes`
- `repeated_report_save_preserves_human_edits_and_existing_files`
- `report_save_failure_is_visible_and_retryable_without_mutating_the_report`
- `saving_a_report_never_changes_its_preview_episode_or_graph_history`
- `remote_report_save_publishes_to_the_canonical_state_host`

## Failure means

RCP still has a special result-view selector, record, or Experiment-only UI;
selection sends immediately, silently forces Work, or always asks the agent to
edit; Work overwrites before Accept, revision creates a second artifact card,
Reject changes the source, a conflict overwrites an external edit, an unresolved
candidate is cleaned or transferred, or Keep freezes bytes, writes under
`.research/`, or overwrites an existing file. Report saving silently fails, shows
a path that does not resolve to the copy, overwrites human files, changes the
stored report or episode, or saves to a local cache instead of remote canonical
state.
