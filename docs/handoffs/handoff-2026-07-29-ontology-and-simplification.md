# Handoff — ontology v0.5 and pre-landing simplification

**Date:** 2026-07-29
**State:** all landed work is green (`223 passed`, `ruff` clean). **No UI-level
verification has been done.** Three items are outstanding, listed at the bottom.

This session did two things: wrote the v0.5 ontology blueprint, and started
simplifying the codebase *before* implementing it. Nothing in v0.5 is
implemented — the blueprint is the target, the code is not there yet.

---

## 1. What is on disk now

### Blueprint

`docs/research-control-panel-blueprint-v0.5.md` (~3,270 lines), superseding v0.4.
Read §0 for the full changelog. The load-bearing parts:

- **§1.0 North star** — the three stages (personal OS → multiplayer → federated),
  and an explicit bound on what they may claim from v1: only the ontology's shape
  and the meaning of the patch log, because those are the expensive-to-reverse
  decisions. Everything else stays out.
- **§5.1** — `confidence` removed from `BaseNode`; `Hypothesis.scope` added as a
  human-authored field; `Evidence.origin` added.
- **§5.4** — `RELATION_SPEC`: per-relation allowed source/target types and a
  layer, replacing the prose "typical shape" table. Ships at `flag`.
- **§5.5** — the epistemic and action layers as two overlay projections over one
  graph, joined at `Experiment`.
- **§5.6 / §5.7** — schema evolution and the extension model, and the
  transferable unit. **Marked as intent, not v1 scope.**
- **§5.8** — rejected alternatives with reasoning, so they are not re-proposed.
- **§6.4** — structural vs. authoring rule sets, the causation rule, and §6.4b
  replay semantics (halt, never skip; no replay cache).

### Code changes that landed

| Area | Change | Result |
|---|---|---|
| `core/materialize.py` | `model_copy(deep=True)` → `_fork_state` (copy containers, share contents) | replay **161× faster** at 800 patches |
| `core/validation.py` | single 930-line module → `core/validation/` package, one function per op behind a registry | largest restructured function 345 → 48 lines |
| `sources/` | new stdlib-only `record_parsing.py`, shipped as source over SSH | `indexer.py` 1195 → 1059 lines, one parser instead of three |
| `storage.py` | `prune_operational_storage` prunes payloads, never run rows | 117 → 63 lines, lineage BFS gone |
| `api/app.py` | shared plumbing extracted from the two run streams | 307/287 → 277/252 lines |

`AGENTS.md` was updated: invariant 7b (materialization never mutates in place),
invariant 10 clarified (shared plumbing fine, shared discriminator not), two
Human preferences (central config; measure before adding *or* removing a cache),
one Repeated failure (never hand-transcribe code into a remote string literal).

---

## 2. Verified vs. not verified

**Verified, by me, on the settled tree:**

- `uv run pytest` → 223 passed. `uv run ruff check src tests` → clean.
- Replaying the demo project's real patch log reproduces its committed
  `graph.json` with **zero differing fields** — checked before and after the
  materialize and validation changes.
- The shipped remote parser, run on a bare interpreter in a subprocess against a
  temp fixture, produces **byte-identical** records to the local path (including
  a non-dict `payload` and the digest-fallback id).
- No `DELETE FROM graph_runs` exists anywhere; provenance receipts default to
  `summary` tier and the pruner only deletes `diagnostic`/`trace`.
- AST scan of all four extracted stream helpers: **no** `kind`/`is_chat`/
  `surface` parameter, and no body reference to one.

**Not verified — this is the gap:**

- **Nothing has been driven at the UI level.** Per `AGENTS.md` that is required
  for changes of this size, and it has not happened. See outstanding item 3.
- **No remote/SSH path was exercised.** The `record_parsing.py` extraction
  changes code that only ever runs on a remote host. The equivalence test proves
  the program is correct on a local interpreter; it does not prove the SSH
  invocation still works end to end. This needs a reachable host.
- Agent runs (seed, refresh, chat, coach) have not been exercised against a real
  provider since the `app.py` refactor.

---

## 3. Outstanding work, in order

### (1) Delete the checkpoint module — decided, not done

The user approved deleting `src/rcp/history/checkpoints.py`. **The blueprint has
already been updated to describe its absence** (§6.4b, §6.7, the §0 changelog,
and the acceptance-test row), so docs and code currently disagree — that is the
first thing to fix.

- Delete `src/rcp/history/checkpoints.py` and `tests/test_history_checkpoints.py`.
- Rewire `src/rcp/history/manager.py` — it loads a checkpoint at `materialize()`
  and saves one via `_save_latest_checkpoint`. Replay becomes a single full pass.
- `ReplayConfig` lives in `checkpoints.py` and is used by `manager.py`; decide
  whether it survives as a plain argument bundle or dissolves into parameters.
- `tests/test_api.py` references checkpoints — check before deleting.

Rationale, so it is not re-litigated: after the `_fork_state` fix, full replay is
~280 ms at 2,000 patches and ~1.2 s at 5,000, of which read-and-parse is a third
and is paid with or without a cache. The cache saved under a second at a scale
most projects never reach, in exchange for a second stored answer to "what does
the log mean."

### (2) Central limits config — agreed, not started

Collect the tunables now scattered across `storage.py`, `agents/context.py`,
`history/delta.py`, `sources/cache.py`, `sources/indexer.py`, `web_assets.py`,
`__main__.py` (limits, timeouts, retention windows, cache bounds).

**Do not centralize schema constants** — `NODE_PREFIXES`, `SLUG_RE`,
`IDENTIFIER_RE`, `IMMUTABLE_NODE_UPDATE_FIELDS`, `HUMAN_EDITABLE_NODE_FIELDS`,
`SourceRef.excerpt`'s 800-char cap. Those are the contract, not knobs, and belong
next to the models they constrain.

This must be done **serially** — it touches files that were owned by four
different parallel agents.

### (3) UI verification — required before calling any of this done

Nothing above has been seen in a browser. Serve and drive:

```bash
uv run rcp open examples/demo-project/state-repo
```

The human often already has a server on 8421 holding the single-instance lock —
probe `http://127.0.0.1:8421/api/health` and reuse it; to run alongside, use a
spare port **and** a throwaway `RCP_DATA_DIR`. Never kill their process.

What to exercise, and why each one:

| View / action | Why it is at risk |
|---|---|
| Project open, graph renders | `_fork_state` changed how every revision is materialized |
| Runs view, task list, progress | `prune_operational_storage` changed the retention contract and return shape |
| Node chat — send with graph changes **off**, then **on** | `_stream_chat_run` was refactored; confirm the reply renders, and that an `applied_revision` arrives before `done` on the authorized turn |
| Seed or refresh | `_stream_graph_run` was refactored |
| Paper coach | `_stream_coach` picked up one shared helper |
| Pause → resume → retry a run | receipt pruning now depends on tier; the freshness proof reads `summary`-tier receipts |

Check `read_console_messages` and `read_network_requests` alongside
`preview_logs` for each. A route that 500s while the page still renders is a
silent failure.

### (4) Then, and only then: implement v0.5

Suggested order, because later steps depend on earlier ones:

1. **The validator split first** (§6.4). Everything else in v0.5 assumes
   authoring rules can tighten without rewriting history. `OpRule` is already a
   frozen dataclass with defaulted fields, so adding a `structural`/`authoring`
   tag is a one-line addition per registry entry.
2. **Replay halts instead of skipping** (§6.4b) — currently `continue` at
   `materialize.py`, and duplicated in `checkpoints.py` if that still exists.
3. **Field changes** (§5.1) — `confidence` out, `Hypothesis.scope` and
   `Evidence.origin` in. These touch `core/models.py` and `web/src/types.ts`,
   which are shared contracts: land serially, then fan out consumers.
4. **`RELATION_SPEC`** (§5.4) at flag level.
5. **The causation rule** (§6.4) at reject level.

---

## 4. Things not to undo

- **`_apply_patch` must never mutate a contained model in place.** `_fork_state`
  shares node/edge/proposal objects between revisions; an in-place field
  assignment would silently corrupt the previous revision. Guarded by
  `test_patch_failing_part_way_leaks_no_earlier_operation`.
- **`record_parsing.py` may import only the standard library.** It is executed on
  remote hosts that have no venv and no `rcp` package. A test enforces this.
- **No shared stream helper may take a caller discriminator.** Leaving some lines
  duplicated between `_stream_graph_run` and `_stream_chat_run` is the correct
  outcome, not an unfinished cleanup.
- **Never delete a `graph_runs` row.** Resume ancestry walks
  `parent_operation_id`; a missing ancestor fails the turn.
- **`summary`-tier receipts are retained for the life of the run.** The resume
  freshness proof reads `operation_created` and `chat_context_assembled`, both of
  which are written at the default `summary` tier.

## 5. One latent bug that was fixed in passing

The remote index script derived a session's terminal record id as
`inner.get("id") or raw.get("uuid") or raw.get("id")` — a third variant, differing
from both the local and the remote-slice implementations. For Claude records it
preferred a top-level `id` over `uuid`, so a remote `last_uuid` could disagree
with the local id **for the same file** — and that id is exactly what the
cursor/terminal verification compares. It now derives from `normalize_record`
like everything else. Providers do not emit a top-level `id` on Claude records
today, so no observable change is expected, but this would have surfaced as an
unexplained "source changed before its indexed terminal record" error on a remote
host.
