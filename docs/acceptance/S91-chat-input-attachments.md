---
id: S91-chat-input-attachments
status: implemented
tier: live
driver: pytest + browser + ssh
covered_by:
  - tests/test_attachments.py
  - tests/test_chat_prompt_protocol.py
  - tests/test_prompts.py
  - web/tests/agentTasks.test.mjs
  - web/tests/api.test.mjs
  - browser + ssh 2026-08-08 — remote-1 Discuss turn staged, read, and retained metadata
invariants: [3, 4b, 6, 10, 10b, 10c, 10d, 10e]
reported_by: human, 2026-08-08
last_passed: 2026-08-08 — browser drove a text attachment through a Discuss turn
  on remote-1; the provider returned the file token, the human message remained
  unchanged, and task history showed the immutable execution-host path and metadata
---

# A file follows one human turn to the agent and nowhere else

An input attachment is temporary human-supplied context required by one
ordinary chat turn. RCP transfers bounded bytes unchanged, gives the agent
execution-host paths, and never promotes the file into canonical research
state, a repository, durable chat bytes, or a provider-specific attachment.

## UI path

Confirmed by the human on 2026-08-08.

1. Open a full or floating node/project chat. Both presentations use the shared
   composer. Add files through the `+` button, drag-and-drop onto the composer,
   or clipboard paste. Pasted plain text remains ordinary message text.
2. Each selected file becomes a removable chip showing name, detected type,
   size, and **Preparing**, **Ready**, or a visible error. `+` opens the file
   picker directly.
3. Accept PNG, JPEG, WebP, PDF, plain text, Markdown, source code, CSV, TSV,
   JSON, HTML, and SVG. Reject directories, archives, Office documents,
   notebooks, audio, video, and unknown types. HTML and SVG remain untrusted
   source inputs; RCP neither renders them nor fetches their dependencies.
4. Accept at most eight files, 16 MiB per file, and 32 MiB total. Input limits
   use their own constants even when they equal output-artifact limits.
5. Text-only chat remains valid. A selected attachment requires a non-empty
   human message, and Send waits until every selected file is Ready.
6. Selection uploads bytes to an opaque ingress set scoped to the RCP instance,
   project, chat, and client. Removing a chip releases its unclaimed bytes.
7. Send atomically claims that exact set for one logical turn. The task stages
   one immutable batch on the agent's local or configured SSH execution
   machine and verifies every file before provider launch. Partial transfer is
   a visible task failure, never degraded context.
8. The human message remains unchanged. A separate RCP-authored turn block
   gives the agent exact execution-host paths plus display name, detected type,
   and size. RCP sends no file contents, base64, extracted text, local browser
   paths, or provider-specific image arguments.
9. After Send, compact metadata-only rows beneath the human message identify
   that turn's files. They offer no Download action and become **Expired** after
   the normal seven-day run-stage retention window.
10. A later ordinary turn receives only its own new files. Resume and Retry
    reuse the exact saved batch and hashes or fail if identity can no longer be
    proved. A provider handoff transfers the same claimed bytes to the new
    execution host rather than reusing an old host path.
11. Attachments do not change Discuss/Work mode or authority. The turn contract
    labels them temporary and forbids turning attachment-only information into
    graph truth. This provenance boundary is explicitly prompt-enforced because
    the graph schema has no attachment citation type.

## Assertions

- `picker_drop_and_file_paste_share_one_attachment_queue`
- `plain_text_paste_remains_message_text`
- `allowlist_and_three_independent_size_bounds_fail_before_send`
- `upload_set_is_opaque_scoped_unclaimed_and_releasable`
- `one_send_claims_one_set_for_one_logical_turn`
- `provider_launch_waits_for_one_verified_immutable_batch`
- `local_and_ssh_prompts_name_execution_host_paths_only`
- `human_message_is_byte_for_byte_unchanged`
- `no_provider_specific_file_or_image_flag`
- `sent_turn_retains_metadata_not_bytes_or_paths`
- `seven_day_expiry_changes_only_the_human_metadata_row`
- `retry_resume_and_handoff_preserve_content_identity`
- `attachments_never_become_canonical_or_durable_evidence`
- `attachment_only_graph_truth_prohibition_is_prompt_enforced`

## Failure means

The wrong turn receives a file, the human message is rewritten, partial
transfer reaches the provider, bytes silently become durable, an expiring file
is presented as graph provenance, a local path reaches an SSH provider, or the
two chat presentations diverge.
