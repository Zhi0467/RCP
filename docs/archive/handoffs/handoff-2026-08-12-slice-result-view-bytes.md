# Slice — RCP stores the bytes it serves for a result view

> Archived 2026-08-17. Complete:
> [S114](../../acceptance/S114-see-your-results-without-leaving.md) is
> `implemented` under this design — RCP stores the bytes it serves, and the
> rollback snapshot subsystem this slice deleted is gone. Retained only as
> historical execution context.

**Date:** 2026-08-12
**Scenario:** [S114](../../acceptance/S114-see-your-results-without-leaving.md),
whose "Where the served bytes live" section is the confirmed decision this slice
implements.
**Design:** the blueprint's
[Result views](../../research-control-panel-blueprint.md#result-views) section
(0.54, canonical).

Read [`AGENTS.md`](../../../AGENTS.md), then the scenario section above, then
[`result_views.py`](../../../src/rcp/runs/result_views.py) end to end. The feature
already works; this slice changes where one copy of the bytes lives and deletes
what that choice was paying for.

## What you are changing, and why

Today the staged file the agent edits **is** the file RCP serves. Two costs
follow from that single fact:

1. A failed revision could destroy a readable view, so the code carries a
   rollback subsystem — prior bytes checkpointed into a private snapshot
   directory in a bespoke binary format with a magic header before every
   revision launch, restored on rejection or hard interrupt.
2. A remote project reads its view over SSH on **every** request, with
   `Cache-Control: no-store`, and the frontend issues a `HEAD` before the iframe
   `GET` — so two SSH round trips per view per render.

After this slice, RCP validates the file after a turn exactly as it does now,
stores the verified bytes, and serves every view from that stored copy. The
staged file stays the agent's working copy, reached by resuming its own session
at the same stable path. **That property is the point of S114 and must not
change** — do not touch how a revision reaches or edits the file.

The rollback subsystem is then deleted, not documented: a failed revision cannot
damage a copy it never touches.

## Precedent, so you do not invent a shape

`campaign_reports` already stores agent-authored HTML directly in SQLite as
`html TEXT NOT NULL`, under the same
`CHAT_ARTIFACT_MAX_FILE_BYTES` cap in [limits.py](../../../src/rcp/limits.py). The
real measured view was 657 KB. Follow that shape.

## Land these serially, first

1. **The `result_views` column** in [storage/result_views.py](../../../src/rcp/storage/result_views.py). The
   table already records `content_sha256` and `size_bytes`; add the bytes beside
   them. A new column goes in the `CREATE TABLE IF NOT EXISTS` **and** is indexed
   only in the migration block below the `_ensure_column` calls — otherwise every
   existing database fails to open with "no such column" while all tests pass on
   their fresh files. Verify against a copy of a real store, not a new one.
2. **Persist at discovery.** `discover_result_view` already validates the single
   direct regular `.html` file, bounds it, and returns bytes with a digest.
   Store those bytes in the same write that creates or updates the record.

Then fan out.

## Fan-out

| Agent | Files | Owns |
|---|---|---|
| Storage | `src/rcp/storage/result_views.py` | column, migration, persistence, expiry deleting bytes with the record |
| Runs | `src/rcp/runs/result_views.py`, `src/rcp/runs/work.py` | deleting the rollback subsystem, re-pointing the revision no-op check |
| Transport | `src/rcp/transport/run_stage.py` | removing the remote rollback/restore counterparts |
| Service | `src/rcp/api/app.py` | serving from the store; moving the active-revision query out |
| Tests | `tests/test_result_view_*.py`, `web/tests/` | rewriting what the deletion invalidates |

## What to delete

All of it, in [result_views.py](../../../src/rcp/runs/result_views.py):
`persist_result_view_rollback_snapshot`, `read_result_view_rollback_snapshot`,
`clear_result_view_rollback_snapshot`, `restore_result_view`,
`restore_local_result_view_bytes`, `_encode_rollback_snapshot`,
`_decode_rollback_snapshot`, `_rollback_snapshot_payload_limit`, the
`_ROLLBACK_SNAPSHOT_*` constants, and the fd helpers that exist only for them.
Keep `discover_result_view`, `prepare_result_view_slot`, the slot helpers, and
`require_result_view_changed`.

In [work.py](../../../src/rcp/runs/work.py): the snapshot persist before launch, both
restore call sites, and the rollback-snapshot receipts. `require_result_view_changed`
stays but compares the new stage bytes against **the stored copy** rather than a
pre-launch snapshot — that is what still refuses a no-op revision.

In [run_stage.py](../../../src/rcp/transport/run_stage.py): the remote restore
counterpart. Keep the list/read operations, which still fail closed.

## Also in scope, because you are already in the file

`_has_active_result_view_revision` is a 25-line `json_extract` query living in
[app.py](../../../src/rcp/api/app.py). It is policy, and `app.py` is composition and
routes. Move it to a named `storage/result_views.py` method with the same behavior — extract
unchanged, then leave it alone.

## Invariants you must not break

- **Invariant 10c and 10e.** The conversation workspace is still keyed by project
  and chat and reused; the view still lives at one stable path inside it; a
  revision still resumes the same native session. A view is still not a per-turn
  artifact.
- **A revision still edits the file in place.** The 2.59x measurement in S114
  depends on the second turn naming the existing view path. Do not let this
  change put a per-turn directory anywhere near it.
- **Keep still writes through the state workspace lock** to `views/` at the
  repository root, never under `.research/`, never a direct file write. Keep now
  publishes the stored bytes; it must still verify digest and size first.
- **A lost session is still reported plainly.** Never silently redraw.
- **Expiry still expires.** An unkept view's stored bytes go with its record, or
  you have quietly made disposable views permanent.

## Out of scope

Anything that would answer [Q6](../../open-questions.md) — a view still emits no
research action, appends no Patch, spends no revision, creates no Proposal, and
changes no attention count. Do not add a cache in front of the store; the store
*is* the fix. Do not touch the gesture channel except where S114 now records the
Work-mode switch as a promise.

## Done means

S114 passes, driven once against the new design — `pytest`, `browser`, and the
remote half against the configured SSH host, which was reachable on 2026-08-12
(`ssh -o BatchMode=yes -o ConnectTimeout=8 <host> true`). Do not drive the old
design first.

The browser half must show a view rendering in the run detail, a gesture becoming
a draft, a revision editing in place with no second card, and Keep. Check
`read_console_messages` and `read_network_requests` alongside `preview_logs`.

Backend `uv run pytest` and `uv run ruff check src tests`; web
`npm --prefix web run build` and `npm --prefix web test`; then `git add -A` and
`uv run pre-commit run --all-files`.

**Coordination note:** `work.py` was being edited by a concurrent session on
2026-08-12 for orchestrator work. Check the working tree is settled before
starting, and scope hook runs to your own paths if it is not.
