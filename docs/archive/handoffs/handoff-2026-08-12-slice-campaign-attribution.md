# Slice — a campaign's work says which campaign it was

> Archived 2026-08-17. Complete:
> [S113](../../acceptance/S113-campaign-attribution.md) is `implemented`, last
> checked 2026-08-15. The S77/S78 orchestrator work this slice sat on top of
> landed first, as the sequencing note asked. Retained only as historical
> execution context.

**Date:** 2026-08-12
**Scenario:** [S113](../../acceptance/S113-campaign-attribution.md), human-confirmed
2026-08-12. Its "Decided" section is the contract; do not re-derive it.
**Design:** the blueprint's attribution paragraph (0.54, canonical) and
[S99](../../acceptance/S99-attribution-travels-with-history.md), which made the base
envelope canonical.

Read [`AGENTS.md`](../../../AGENTS.md), then S113, then
[`manager.py`](../../../src/rcp/history/manager.py)'s
`_stamp_attribution_for_admission`.

## What you are building

One nullable field on the Patch envelope, stamped by RCP, plus the History
surface that makes it worth having. That is the whole slice. Resist every
temptation to add a second field.

## What already exists, so you do not rebuild it

- `Patch` in [models.py](../../../src/rcp/core/models.py) already carries
  `authorized_by`, `profile` (`"ordinary" | "orchestrator"`), `task_id`, and
  `project_identity`.
- `agent_task_authority` in [storage/agent_tasks.py](../../../src/rcp/storage/agent_tasks.py) resolves
  attribution from the producing task's own `graph_runs` row — **and that table
  already has a `campaign_id` column.** Stamping is one more column in one
  existing `SELECT`. Admission never needs the `campaigns` table.
- `AgentTaskAuthority` and `AgentDispatchAuthority` in
  [authority.py](../../../src/rcp/core/authority.py) are where the resolved binding
  travels.
- `RevisionSummary` in [delta.py](../../../src/rcp/history/delta.py) already carries
  `authorized_by`, `profile`, and `task_id` onto the wire, and
  `web/src/types.ts` already mirrors them.
- `campaign_reports` already stores the wrap-up HTML and is already served by a
  preview route in [app.py](../../../src/rcp/api/app.py).

## Land these serially, first

1. **`campaign_id` on `Patch`** in [models.py](../../../src/rcp/core/models.py) — a
   shared contract, so it lands alone, before anything fans out.
2. **`web/src/types.ts`** for `RevisionSummary`.

Then fan out.

## Fan-out

| Agent | Files | Owns |
|---|---|---|
| History | `src/rcp/history/` | stamping, the refusal, the projection field |
| Storage | `src/rcp/storage/agent_tasks.py` | carrying `campaign_id` on the resolved task authority |
| Service | `src/rcp/api/app.py` | decorating a group header with live campaign state |
| Web | `web/src/components/ProjectHistoryDrawer.tsx` | the profile label fix, grouping, the report link |
| Tests | `tests/`, `web/tests/` | S113's asserts |

## The rules, precisely

**Every patch produced inside the campaign carries it** — orchestrator and seated
worker alike. A worker patch reads `profile: "ordinary"` **and** a `campaign_id`.
That pairing is not a contradiction to be tidied away; it is the exact truth, and
a reviewer who "fixes" it has removed the point of the field.

**Refuse an `orchestrator` patch with a null `campaign_id`** at admission,
alongside the existing rule that a supplied value must equal the canonical one.
Do **not** refuse on the campaign record being absent — that would reintroduce
the live lookup this design removes, and risks discarding a completed agent turn
over a missing row.

**A human approval patch carries none.** `_stamp_attribution_for_admission`
already nulls `profile` and `task_id` for `kind == "approval"`; `campaign_id`
joins them.

**The field is inert.** Nothing in validation, admission authority, or any
permission decision may read it. Assert this behaviorally: vary the field across
`None`, a real id, and a garbage id, and every verdict must be byte-identical.

## The History surface

[ProjectHistoryDrawer.tsx](../../../web/src/components/ProjectHistoryDrawer.tsx)
currently prints the literal string **"Ordinary Agent task"** on every agent
revision, ignoring `summary.profile`, which is already on the wire. An
orchestrator's patch renders today as "Ordinary". Fix that first; it is a lie
independent of everything else here.

Then group a campaign's revisions under one header. **The grouping comes from the
envelope alone**, so it can never break. The header is then decorated with live
campaign state — running, stopped, exhausted, failed, completed — and degrades to
"campaign · authorized by X · date · N revisions" when the record is gone. That
decoration happens in the API after replay, never inside the replay observer,
because replay may not load live campaign, task, membership, or permission
records.

One control on the header opens the campaign's wrap-up report — the same document
the Runs row opens, through the same sandboxed frame.

## Invariants you must not break

- **Invariant 1.** Patches are append-only. This is additive; base and legacy
  patches are never rewritten and must keep rendering as they do.
- **Invariant 3.** RCP supplies the field; neither the request nor agent output
  may set it. An agent cannot name its own campaign.
- **Replay stays offline.** It reads the envelope and nothing else.
- **No new permission principal.** The id is a label in history. If any code path
  starts branching on it for authority, the slice has failed.

## Out of scope

Parent-task and worker fields in the envelope, a receipt-schema change, filtering
History down to one campaign, any control that acts on a campaign from inside
History, and copying the report into the state repository. The operational store
already holds parent and worker lineage and never prunes those rows.

## Done means

S113 passes: `pytest` for the envelope, stamping, refusal, inertness, and replay;
`browser` for the profile label, the grouping, the degraded header, and the
report opening from the group.

Backend `uv run pytest` and `uv run ruff check src tests`; web
`npm --prefix web run build` and `npm --prefix web test`; then `git add -A` and
`uv run pre-commit run --all-files`.

**Sequencing note:** this slice sits directly on top of the S77/S78 orchestrator
work, which was still being implemented by a concurrent session on 2026-08-12.
Start it after that lands, or the `graph_runs.campaign_id` values you stamp from
will be moving underneath you.
