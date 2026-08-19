# Active implementation handoffs

- [Backend structural refactor work order](handoff-2026-08-18-backend-structural-refactor.md)
  — the fact-checked, human-confirmed phase order and target architecture.
- [2026-08-19 backend structural-refactor pickup](handoff-2026-08-19-backend-structural-refactor-pickup.md)
  — the exact `ed4c019` stopping point after Phases 0–6 and Phase 7's durable
  admission/launch boundary, including verification, unimplemented work, and
  decisions to discuss before startup reconciliation.

[`rcp_architecture_audit.md`](rcp_architecture_audit.md) is the evidence behind
that handoff, not a work order. Its Appendix A explains the findings from
scratch.

This directory is reserved for human-confirmed work that is ready to implement
but not yet implemented and verified. A handoff may refine an explicitly open
implementation detail, but it may not silently change
[`../design.md`](../design.md) or the applicable file in
[`../specs/`](../specs/). Archive a handoff when its implementation closes or it
is superseded or abandoned.
