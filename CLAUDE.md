# CLAUDE.md

@AGENTS.md

## Claude-specific notes

- Read files and plan yourself. Make small edits directly. Delegate larger
  implementation along module boundaries using concurrent independent Agent calls.
  Keep shared-contract edits serial (`src/rcp/core/models.py`, `src/rcp/config.py`,
  `web/src/core/types.ts`). Retain verification and complete diff review.
- Give each subagent file ownership, invariants, and a check command.
  Rerun the checks yourself.
- The human often has a server running on 8421 that holds the single-instance
  lock. Probe `http://127.0.0.1:8421/api/health` first and reuse it. To run your
  own alongside, use a spare port and a throwaway `RCP_DATA_DIR`. Never kill
  the human's process.
- In UI work, avoid unnecessary secondary labels and descriptions. Let the
  layout do the explaining, put a truly needed note on hover, and keep motion
  smooth (AGENTS.md Verification).
- Use `uv run …` and `npm --prefix web …`. Do not activate the venv or change
  into `web/`.
