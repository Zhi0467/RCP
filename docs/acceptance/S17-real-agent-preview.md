---
id: S17-real-agent-preview
status: implemented
tier: live
driver: browser
covered_by: none
invariants: [4, 10, 10b, 11]
last_passed: 2026-07-30 — live browser with Codex 0.145.0 and Claude Code 2.1.219 using Haiku
---

# A real provider produces the same preview

Mocks cannot prove that current Codex and Claude versions accept the launch
flags, understand the artifact-directory instruction, preserve the labelled
answer, and write an openable file.

Run this before declaring the feature complete for each provider RCP reports as
ready on the test machine. Assertions concern only protocol shape, never answer
wording or visual polish.

## UI path

Use ordinary Node chat or Project chat. Ask for an explanation with a small
interactive HTML preview, leaving **May change the graph** off. The Markdown
reply appears normally, followed by one artifact row. Selecting it opens the
preview in a new browser tab. The row also offers Download without turning the
file into RCP state.

## Drive

For Codex, then Claude:

1. Start an unauthorized chat turn asking for a short Markdown explanation,
   fenced code, and a minimal HTML preview.
2. Close the chat while the task runs, then reopen it after completion.
3. Open the attachment and interact with one control in the generated HTML.
4. Follow one HTTP(S) reference link from inside the preview, then use the RCP
   Download action and compare the downloaded file with the scratch source.
5. Reload RCP and reopen the same conversation while its scratch stage remains.
6. Remove or age out only the preview file, then try both Open and Download.

## Assert

- `provider_cli_accepted_flags`
- `provider_cwd_is_conversation_scratch`
- `answer_is_labelled_final_message`
- `markdown_and_both_code_fences_render`
- `artifact_uses_rcp_directory` — no provider-owned artifact location or
  provider-specific inline directive is needed
- `graph_revision_unchanged`
- `preview_opens_outside_chat`
- `preview_interaction_works`
- `reference_link_requires_click_and_leaves_rcp_open`
- `explicit_download_matches_source`
- `panel_close_and_reload_preserve_descriptor`
- `expired_file_makes_open_and_download_unavailable`
- `reply_unchanged_after_preview_failure`
- `no_console_error_or_server_traceback`

If a provider advertised as ready cannot complete this scenario, the feature is
not complete for that provider/version. RCP does not silently fall back to a
different artifact protocol.

## Failure means

The design works only against the fake launcher or one provider/version, or the
frontend has coupled attachment success to rendering the answer.
