---
id: S16-chat-artifact-contract
status: implemented
tier: hermetic
driver: pytest
covered_by:
  - tests/test_api.py::test_chat_artifacts_are_bounded_sandboxed_and_independent
  - tests/test_api.py::test_authorized_chat_applies_its_patch_with_an_artifact_present
  - tests/test_api.py::test_chat_artifact_discovery_enforces_every_central_bound
  - tests/test_api.py::test_unexpected_artifact_discovery_error_does_not_fail_chat
  - tests/test_api.py::test_resumed_artifact_directory_rejects_a_symlinked_scope
  - tests/test_api.py::test_retried_chat_gets_a_new_artifact_scope_in_the_same_conversation_stage
  - tests/test_api.py::test_failed_chat_task_retains_artifacts_emitted_before_the_error
  - tests/test_storage.py::test_agent_task_result_retains_only_valid_bounded_artifact_descriptors
  - web/tests/chatMarkdown.test.mjs
  - web/tests/agentTasks.test.mjs
invariants: [10, 10b, 10c, 11]
---

# A preview is optional; the answer and graph are not

A chat answer is the provider's labelled final assistant message. RCP renders
that answer as Markdown. Previewable files are separate, temporary attachments:
they can enrich the answer, but their absence or failure cannot change the
answer, the task verdict, or graph authority.

## UI path

- The existing agent reply renders as Markdown. Fenced code always renders as a
  code block, including a language name the renderer does not recognize.
- Zero or more compact artifact rows appear immediately below the reply that
  produced them. The first supported formats are HTML and browser-safe raster
  images: PNG, JPEG, GIF, and WebP. SVG, PDF, audio, video, and arbitrary files
  are deliberately not previewed in this first cut.
- Selecting an available row opens an RCP-owned preview URL in a new browser
  tab. The artifact is not embedded in the conversation.
- HTML may run its own inline JavaScript inside an opaque child frame. It cannot
  access or navigate RCP, open popups, submit forms, start downloads, or use
  ordinary network resource APIs. An inline script may navigate its own isolated
  frame, which can issue a navigation request without replacing RCP. A real user
  click may follow an HTTP or HTTPS reference link by navigating the preview tab
  itself; `file:`, `javascript:`, `data:`, and other schemes do not become
  openable links. The presence of an external URL never causes the artifact to
  be rejected. For example, `<a href="https://google.com">` renders and works
  when clicked, while an external `<script src>`, image load, or `fetch()` is
  blocked at runtime. The preview content must otherwise be self-contained.
- If the file has expired or cannot be reached, the row remains attached to the
  correct turn but becomes **Preview unavailable**. The reply does not change.
- Each available row has an explicit **Download** action for its original file.
  Download is user-initiated and streams the bounded source bytes; it does not
  make RCP retain or import the artifact. There is no Save, Publish, or
  add-to-project action in this feature.

## Setup

A temporary project and scripted agent. Run the same cases with the provider
field set to Codex and Claude; the scripted output and RCP result shape are
identical.

For one Discuss turn, the agent:

1. emits a labelled Markdown answer containing a heading, list, a known-language
   code fence, and an unknown-language code fence;
2. writes `patch.json` despite Discuss having no graph authority;
3. writes valid HTML and image files into the exact per-turn artifact directory;
4. leaves nested, unsupported, over-limit, symlinked, and malformed candidates
   beside them.

Additional cases write no artifacts, make artifact discovery fail, remove an
already-described artifact, and run an otherwise identical Work turn with a
valid graph patch.

## Drive

Start each turn through the normal background-task API, let it reach a terminal
state, inspect the persisted task result, and request every described preview
through the RCP artifact endpoint. Never give the endpoint a filesystem path.

## Assert

- `answer_is_labelled_message_verbatim` — within the existing bounded-result
  contract, artifact handling does not rewrite, replace, further truncate, or
  parse the conversational reply
- `answer_without_artifact_succeeds`
- `artifact_failure_does_not_fail_answer` — discovery, validation, reading, or
  preview generation errors become bounded diagnostic receipts only
- `scratch_is_writable_in_discuss` — both provider launch adapters permit
  disposable scratch output even though Discuss has no graph contract
- `discuss_patch_discarded` — revision and append count remain unchanged
- `work_patch_unchanged` — freshness, scope, validation, and one-revision
  behavior still hold when a Work reply also has artifacts
- `one_artifact_directory_per_turn` — the conversation keeps its stable native
  session workspace, while each task receives a distinct RCP-created artifact
  directory inside it; Resume reuses that task directory and Retry gets a new one
- `filesystem_is_the_provider_contract` — RCP does not parse Codex directives,
  Claude conventions, provider visualization directories, or URLs from the
  answer
- `only_allowed_regular_direct_children` — no traversal, nested file, symlink,
  unsupported type, or candidate beyond the centrally configured count,
  per-file, or total-byte bounds becomes an attachment
- `descriptors_not_payloads` — the durable task result stores bounded attachment
  descriptors, not file bytes, HTML, remote paths, or stage paths
- `unknown_artifact_is_not_found` — a client cannot use the endpoint as a file
  browser
- `missing_artifact_is_gone` — a descriptor whose temporary file disappeared
  reports unavailability without changing the stored task result
- `html_is_sandboxed` — the browser receives an RCP-owned wrapper with a
  restrictive content-security policy and sandboxed document; inline scripts
  work, ordinary network resource APIs are blocked, self-frame navigation cannot
  replace RCP, and the agent HTML is never trusted as the parent RCP origin
- `reference_navigation_requires_user_activation` — an HTTP(S) anchor may
  navigate only the already-open preview tab after a real click; scripts cannot
  redirect the RCP tab, create popups, or open non-web schemes; merely containing
  the link never rejects or hides the preview
- `images_are_nosniff` — raster responses use their validated media type and
  disable MIME sniffing
- `download_is_explicit_and_exact` — the RCP control returns the original
  validated bytes with attachment disposition only after the human selects
  Download; preview code cannot invoke that action for itself
- `no_second_artifact_copy` — successful discovery does not copy the file into
  canonical state, the chat transcript, or durable app storage

## Specification amendments required before code

Confirmation of this scenario authorizes these narrow documentation changes:

1. In invariant 4b and the blueprint's structured-deliverable contract,
   `patch.json` remains the only agent-to-RCP
   **graph-change** channel, while the per-turn artifact directory becomes an
   optional non-canonical preview channel.
2. In invariant 10d and S40, Discuss keeps canonical and repository inputs
   read-only while scratch remains writable. Work may edit exact run-scope
   repositories and may emit an optional graph patch.
3. Successful ingest scratch remains disposable. A successful chat's existing
   conversation workspace remains temporary until its normal stage retention
   expires, so previews can be reopened while that workspace still exists.

## Failure means

The preview path gained authority over the answer or graph, exposed arbitrary
scratch files, smuggled provider-specific behavior into RCP, or turned a
temporary attachment into durable project state.
