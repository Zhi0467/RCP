# Active implementation handoffs

- [Backend structural refactor](handoff-2026-08-18-backend-structural-refactor.md)
  — one lifecycle for agent tasks, a snapshot that cannot be half-built, a narrow
  transaction scope, deduplicated rules, and the three largest backend files
  (`api/app.py`, `background.py`, `runs/work.py`) split along real seams.

[`rcp_architecture_audit.md`](rcp_architecture_audit.md) is the evidence behind
that handoff, not a work order. Its Appendix A explains the findings from
scratch.

This directory is reserved for human-confirmed work that is ready to implement
but not yet implemented and verified. A handoff may refine an explicitly open
implementation detail, but it may not silently change
[`../design.md`](../design.md) or the applicable file in
[`../specs/`](../specs/). Archive a handoff when its implementation closes or it
is superseded or abandoned.
