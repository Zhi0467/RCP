---
id: S44-chat-and-ingest-boundaries
status: pending
tier: local
driver: pytest + browser
covered_by:
  - tests/test_api.py::test_chat_does_not_assemble_or_project_transcripts
  - tests/test_api.py::test_chat_launch_exception_keeps_workspace_without_transcript_projection
  - tests/test_api.py::test_graph_stream_launches_with_degraded_source_fallback
  - tests/test_sources.py::test_refresh_source_failure_keeps_provider_fallback_context
invariants: [4b, 5, 8, 10, 10b, 10e, 11]
---

# Chat is not transcript ingestion

## User promise

Discuss and Work are ordinary agent invocations over the project graph, the
focused node, the user's request, and the exact repository scope. They do not
read, index, copy, prompt with, validate against, or authorize from prior chat
transcripts. Their current answer may still be written to the canonical chat
history for the UI after the provider returns. Exact repository scope describes
the context RCP supplies: Discuss remains read-only, while Work tooling and
repository access are unrestricted.

Seed and Refresh are separate ingestion runs. If RCP cannot assemble transcript
metadata, the run remains launchable: the provider receives the named provider
sources, the last accounted coverage boundary, and an explicit warning so it
can inspect those sources itself. RCP does not invent coverage for sources it
could not read.

## UI path (proposal)

- Open a node or project chat and send a **Discuss** turn.
- Confirm the provider receives graph/current-node context and the exact
  repository scope, with no transcript pointers or transcript paths.
- Send **Work** and confirm it keeps the same contextual scope while using
  unrestricted repository/tooling access and its optional semantic graph-patch
  authority.
- Start a **Seed** or **Refresh** run while one provider source is unreadable.

## Assertions

- Discuss and Work launch without transcript projection or transcript-pointer
  validation, and a chat turn does not fail in transcript staging.
- Discuss can read exact run-scope repositories. Work receives those same
  pointers as context, but they do not limit its unrestricted repository/tooling
  permissions. Neither mode gets provider-root directories through chat.
- Seed/Refresh launch with an observable source warning and provider/source
  fallback, including the last accounted boundary; no false coverage is
  recorded.
- The completed chat answer is preserved for the UI as history, but is not fed
  back into the same or later agent context by this path.
