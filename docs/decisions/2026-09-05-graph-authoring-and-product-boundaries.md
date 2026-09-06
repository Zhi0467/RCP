# Retire the open-question register with explicit product boundaries

Confirmed by the human 2026-09-05. This record preserves decisions likely to be
reopened accidentally, not a second backlog or a claim that proposed features ship.

- **Glossary:** thin, revisable project-wide definitions authored by graph-writing
  agents through ordinary Patches; preserve inline rendering. Definitions are
  not nodes. Authority lives in [the authority spec](../specs/authority-and-proposals.md).
- **Watchers:** completion is the deliberate wake boundary. Keep observation
  failure handling and retained completed/stopped/degraded history; no
  intermediate-output wake or extra record-cleanup feature. See
  [watcher resources](../specs/conversations-episodes-and-watchers.md#watcher-resources).
- **Parallel work:** do not introduce an exclusive repository lease. Shared
  checkout tasks remain concurrent. Composer worktree selection and explicit
  Git merge-back need their own design in [draft PR #48](https://github.com/Zhi0467/RCP/pull/48),
  separate from RCP's research-graph branches.
- **Quality advice:** use programmatic nonblocking flags in the existing validator.
  Reject the separate mandatory scanner proposal S59, not optional graph-review
  skills. Do not pretend a deterministic rule can judge scientific meaning.
- **Human graph editing:** permit general node/edge editing with preview and
  Sync, preserving history on removal. Reject artifact-selection-to-Evidence
  shortcuts; the viewer remains ordinary chat context, and WebMCP gains no
  human-judgment authority. See [graph projections](../specs/api-web-and-desktop-projections.md).
- **Domains:** RCP's core is general-purpose. Data interaction, visualization and
  domain connectors may be future extensions. Do not impose a research-field
  allowlist or claim supported domains from speculative rankings; describe actual
  capabilities and complement external specialized tools.
- **Live steering:** desirable if it is a modest extension of the provider-call
  model. [Draft PR #49](https://github.com/Zhi0467/RCP/pull/49) owns feasibility and
  further discussion, including local/SSH delivery and recovery. Inbound messages
  during a turn do not inherently require a persistent session daemon.
- **Peer mail:** no cross-episode/worker-to-worker mail. Preserve the existing
  orchestrator/worker star topology and recipient budget/authority boundaries.
- **Restore:** add no client rollback detector. Preserve the existing restore
  safety procedure without suggesting that matching `space_id` detects an older
  snapshot. See [server operations](../specs/server-and-machine-operations.md#backup-and-restore).

The superseded register is [historical evidence only](../archive/open-questions-2026-09-05.md).
Do not restore its unresolved statuses as current instructions. Future feature
work requires concrete scope and its owning specification, not a revived register.
