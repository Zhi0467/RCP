# Dispatch — an agent can wait on the graph

**Date:** 2026-08-12
**Scenario:** [S76](../acceptance/S76-graph-condition-wake.md) — confirmed by the
human 2026-08-12, including the closed two-condition vocabulary.
**Design:** [wake handoff](handoff-2026-08-07-graph-condition-wake.md), and the
blueprint's [Watch delivery](../research-control-panel-blueprint.md#watch-delivery)
section, which now records this.

Read [`AGENTS.md`](../../AGENTS.md) first, then the scenario, then
[`watchers.py`](../../src/rcp/watchers.py). Everything below is settled; nothing
here needs another design decision.

## What you are building

An agent finishes its turn, declares what would wake it, and terminates. Today
it can only name a shell command. After this, it can also name a fact about the
canonical graph: a node reaching one of a set of statuses, or a Proposal on a
node being resolved. When that becomes true, its conversation wakes exactly
once.

Two conditions. That is the whole vocabulary. Standing changes, edge
predicates, new-node arrivals, and arbitrary queries were each offered to the
human and declined.

## Land this serially, first

`watch.json` is an agent-facing contract and the watcher record is a stored
schema. Both are shared, so they do not fan out.

1. **The file shape.** `watch.json` becomes an object with two named lists:

   ```json
   {
     "external": [{ "check_command": "…", "log_path": "…", "cwd": "…" }],
     "graph": [{ "node_id": "blk/foo", "status_in": ["resolved"] }]
   }
   ```

   All-or-none validation applies to the file as a whole — one invalid item in
   either list arms none. **Both** lists empty is the exit declaration that
   requires success, a Proposal, or a Blocker in the same Patch. Existing strict
   item validation for `external` is unchanged.
2. **The record.** A separate watcher type, not a variant with three dead
   fields. It shares the existing `WatcherBinding` — provider, session, host,
   stage, conversation, control node — because delivery is identical.
3. **The migration.** A new column is declared in `CREATE TABLE IF NOT EXISTS`
   *and* indexed. Index it **only** in the migration block below the
   `_ensure_column` calls, or every existing database fails to open with "no
   such column" while all tests pass on their fresh files. This has bitten this
   repo before. Verify by opening a copy of a real store, not a fixture.

Then fan out.

## Fan-out

| Agent | Files | Owns |
|---|---|---|
| Watchers | `src/rcp/watchers.py`, `src/rcp/background.py` | condition evaluation, firing, coalescing, startup sweep |
| Loop wiring | `src/rcp/runs/experiment_loop.py`, `src/rcp/agents/experiment_loop_prompt.py` | reading the two lists, the arming path, contract prose |
| Tests | `tests/` | the scenario's checks |

## Invariants you must not break

- **Never route a graph condition through `WatcherPoller`.** Canonical state
  changes only at revision boundaries. Evaluate after a patch applies, after a
  human Sync, and once at startup so a condition satisfied while RCP was down
  still fires. Do not invent a shell command that introspects the graph.
- **Fail closed.** If replay has halted or materialization is degraded, a
  condition does not fire. That is *not yet*, never completion. A condition on a
  removed node is terminally retired.
- **Canonical only.** A staged but unsynced draft never fires a condition.
- **Every wake spends one invocation unit**, including when the human's own Sync
  satisfied it. This is what makes budget exhaustion a termination guarantee;
  one free wake and it stops being provable.
- **Invariant 10c still applies.** A wake clears the previous turn's
  `patch.json` and `watch.json` fail-closed.
- **Invariant 10g still applies.** The wake resumes the episode's validated
  native-session binding under the existing continuation cause. Nothing falls
  back to a fresh session silently.

## One thing to verify rather than assume

If an external watcher and a graph condition complete together, they must
deliver as **one** wake. The existing atomic claim — queue creation, budget
admission, and the notified ledger committing together — probably generalizes,
but confirm it against the code rather than assuming. This is a check to run,
not a decision to make.

## Out of scope

- The `watch-graph` **command** on the staged client. It is orchestrator-only
  and belongs to a later piece. Loops arm by file, and there is deliberately one
  spelling per caller.
- Any cross-conversation delivery. The waker and the wakee are the same
  conversation and episode.
- A third condition type.

## Done means

S76 passes: `uv run pytest`, plus `uv run ruff check src tests`, then
`uv run pre-commit run --all-files` with `git add -A` first so new files are
actually seen. Stamp the scenario when it passes and flip its status to
`implemented`.
