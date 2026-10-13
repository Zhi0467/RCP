# Retire the open-question register with explicit product boundaries

These cross-cutting choices constrain new features; current behavior belongs to
the linked owners.

- [Watchers](../specs/conversations-episodes-and-watchers.md#watcher-resources)
  wake on completion, not intermediate output. Preserve observation failures and
  completed/stopped/degraded history without a separate cleanup feature.
- [Parallel repository work](../specs/conversations-episodes-and-watchers.md)
  needs no exclusive repository lease. Shared-checkout concurrency and explicit
  code integration are separate from graph branching.
- [Graph editing](../specs/api-web-and-desktop-projections.md) uses preview and
  Sync. Artifact selection is conversation context, not an Evidence-creation or
  human-judgment shortcut.
- [Provider steering](../specs/providers-and-containment.md#live-human-steering)
  extends the existing ordinary chat attempt, not a persistent provider daemon.
  [Agent mail](../specs/auto-research-and-branch-merge.md#mail-and-lifecycle-notices)
  stays within an orchestrator/worker star, without peer or cross-episode mail.
- [Restore](../specs/server-and-machine-operations.md#backup-and-restore) uses
  its explicit safety procedure. Matching `space_id` does not detect an older
  snapshot and must not be presented as a client rollback detector.

RCP's core is general-purpose. Domain connectors and visualization may extend it;
there is no research-field allowlist or speculative claim of supported domains.
[Glossary and quality advice](../specs/graph-history-and-transitions.md#typed-graph-operations)
belong to the graph spec: definitions are metadata, and deterministic advice
cannot judge scientific meaning.
