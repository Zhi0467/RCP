# Research Control Panel blueprint v0.8 amendment

This amendment supersedes the earlier chat-transcript projection language in
the blueprint. It is intentionally narrow: it defines the boundary between a
conversation turn and source ingestion without changing graph, repository, or
paper semantics.

## Chat and source ingestion are different operations

Discuss and Work are agent invocations, not transcript-ingestion runs. Their
assembled context contains the project graph, the focused node when one is
selected, the current user request, and the exact repository scope for the
turn. Discuss receives those repositories read-only. Work receives the same
repositories with its existing exact-scope write permission and its optional
validated graph patch channel.

RCP must not read, index, copy, project, include in prompts, validate, or use
for authorization any prior chat transcript as part of Discuss or Work. A
provider continuation/session identifier may be passed to the provider for
normal provider behavior; that is not RCP transcript context or RCP transcript
validation. The answer from the current turn may be appended to canonical chat
history for display, but that write is not an input to the turn.

Seed and Refresh remain the only operations that assemble conversation-source
context, cursors, coverage, and ingestion slices. Source assembly is
best-effort at launch. If an index, pointer, or source file cannot be read,
RCP records an explicit diagnostic and gives the provider the available
provider/source names or roots plus the last accounted coverage boundary. The
provider may inspect those sources directly. RCP must not manufacture a
successful read, cursor advancement, or coverage claim for unavailable input.
