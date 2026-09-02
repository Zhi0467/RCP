# CLAUDE.md

Repository guidance for Claude Code. The canonical instructions live in
[`AGENTS.md`](AGENTS.md) and are imported below — read them before touching this
repo. Do not duplicate content here; edit `AGENTS.md` instead, and keep it
updated as described in its "Maintaining this file" section.

@AGENTS.md

## Claude-specific notes

- **Delegate implementation, keep the rest.** Read the files yourself, make
  single-file edits inline, plan yourself — then hand the implementation to
  parallel subagents along the module boundaries in `AGENTS.md`, issuing the
  independent `Agent` calls in one block so they run concurrently. Verification
  and diff review come back to you. Stay serial for small changes and for edits
  to the shared contracts (`src/rcp/core/models.py`, `src/rcp/config.py`,
  `web/src/types.ts`).
- Give every subagent its file scope, the invariants it must not break, and its
  own check command. Re-run the checks yourself before reporting done.
- The human often has a server already running on 8421, holding the
  single-instance lock. Probe `http://127.0.0.1:8421/api/health` first and reuse
  it; to run your own alongside, use a spare port **and** a throwaway
  `RCP_DATA_DIR`. Never kill their process.
- Use `uv run …` and `npm --prefix web …` rather than activating the venv or
  `cd`-ing into `web/`.
