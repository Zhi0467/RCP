# Research Control Panel blueprint v0.12 amendment

This amendment supersedes v0.8's requirement that Seed and Refresh assemble
conversation-source metadata, per-session cursors, coverage, or ingestion
slices, and supersedes S39's normalized conversation preparation contract. It
does not change the separation between chat and ingestion or the canonical
graph-patch boundary.

## D35 — Provider logs stay where they already are

RCP stores provider log roots in the project manifest and uses the canonical
graph's `last_refresh_at` as the project ingestion watermark. A Seed/Refresh
contract gives the execution-host agent those roots, the watermark, the exact
repository scope, and the optional human request. The agent reads provider logs
in place. When the corpus is large, provider-owned read-only subagents may each
inspect a bounded source question; the parent remains the sole graph-patch
writer.

RCP performs only a bounded existence/readability probe of each configured
provider root. It never parses conversation records for run preparation,
normalizes provider logs, materializes per-session slices, copies native
provider transcripts, transfers conversation content, or validates an agent's
coverage against per-log record cursors. Source failures are explicit contract
warnings and do not manufacture successful coverage.

The watermark is RCP-owned. It advances only when a Seed/Refresh patch applies,
which already sets `GraphState.last_refresh_at` from RCP-owned patch metadata.
Failed, paused, interrupted, or rejected work leaves it unchanged. A new project
has no watermark. The optional human request may narrow a first Seed's historical
period. The watermark is deliberately a project-level incremental-reading hint,
not an exactly-once record cursor; agents tolerate overlap and deduplicate when
provider logs require it.

[`acceptance/S62-direct-provider-log-ingestion.md`](acceptance/S62-direct-provider-log-ingestion.md)
is the executable contract for this amendment.
