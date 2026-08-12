# Dispatch — auto-research campaigns

**Date:** 2026-08-12
**Scenarios:** [S77](../acceptance/S77-auto-research-stops-at-belief.md) and
[S78](../acceptance/S78-one-budget-one-stop.md), both confirmed 2026-08-12.
**Design:** the blueprint's
[Auto-research campaigns](../research-control-panel-blueprint.md#auto-research-campaigns)
section (0.45, canonical) and
[the orchestrator handoff](handoff-2026-08-07-orchestrator.md) for the reasoning
behind each ruling.

Read [`AGENTS.md`](../../AGENTS.md), then the blueprint section, then both
scenarios.

## What you are building

A human presses auto-research in the project header, sets a budget in
invocations, and optionally types a starting instruction. A project-owned
orchestrator then conducts research across the project — creating Evidence,
running Experiments, resolving Blockers, deciding Decisions, seating workers —
until the budget runs out, a human stops it, or it finishes. It never changes an
existing ResearchQuestion or Hypothesis without a human. Every ending produces
one durable report.

## Sequencing: most of this does not wait

The orchestrator is piece 3 of 3. Piece 2 (graph-condition wake) is landing now;
piece 1 (the authority core, [S115](../acceptance/S115-beliefs-change-only-through-you.md))
is dispatched but unbuilt.

Only **the elevated profile's graph authority** depends on piece 1 — it consumes
that work's action table and its widened Proposal vocabulary, and building a
second copy would be a strictly worse duplicate. Everything else is independent
and can start immediately:

- the staged command client, its per-turn credential, its idempotency keys, and
  its event ledger;
- the campaign task, the single budget pot, exhaustion, and Stop;
- the mail channel;
- the report skill.

The `watch_graph` command needs piece 2's condition vocabulary, which lands
first anyway.

## Land these serially, first

1. **The command client protocol** — verbs, exit values, request/response file
   shapes. It generalizes the existing staged live-Patch validator client rather
   than adding a second channel. Ship one stdlib-only module's *source* over SSH,
   as `record_parsing.py` and the validator client already do. Never
   hand-transcribe it into a string literal, and never have it call an HTTP
   endpoint — RCP is not assumed reachable from the execution host.
2. **The campaign record and its budget pot** in `storage.py`. A new indexed
   column goes in `CREATE TABLE IF NOT EXISTS` *and* is indexed **only** in the
   migration block below the `_ensure_column` calls, or every existing database
   fails to open with "no such column" while all tests pass on their fresh files.
   Verify against a copy of a real store.
3. **`web/src/types.ts`** for the campaign row and budget meter.

Then fan out.

## Fan-out

| Agent | Files | Owns |
|---|---|---|
| Client | `src/rcp/transport/`, `src/rcp/agents/` | the staged client, credential, idempotency, event recording |
| Campaign | `src/rcp/runs/`, `src/rcp/background.py` | campaign lifecycle, the one pot, exhaustion, Stop, mail delivery |
| Skill | `src/rcp/skills/`, `src/rcp/skill_registry.py` | the versioned campaign-report skill |
| Web | `web/src/` | header entry, authorization dialog, campaign row, budget meter, mail thread |
| Tests | `tests/`, `web/tests/` | both scenarios' checks |

## Invariants you must not break

- **One campaign per project, one orchestrator profile.** Resist a second
  profile with almost the same authority.
- **Everything spends from one pot** — orchestrator turns, worker turns, and
  every wake. No exceptions is what makes exhaustion a termination guarantee.
  **One unit is reserved for the wrap-up report**, because a report is required
  on every ending and exhaustion is an ending.
- **A seated worker gets no scope of its own.** Where it may be seated is
  bounded — Experiments and Blockers only. What it may then touch is not. Do not
  add a mechanical seat boundary; there is deliberately none.
- **No agent approves a Proposal.** Lineage never confers approval authority.
- **Stop reuses Stop loop exactly.** Do not write a second stop semantics, a
  second budget, or a second wake path. Each already exists and is durable.
- **Messages carry no graph authority and are hearsay.** `patch.json` stays the
  only graph channel; graph facts get read from the graph.
- **Idempotency is deduplication, not recovery.** A key with a record returns the
  existing worker and never restarts it. The dangerous case is the one where the
  first spawn *succeeded*.
- **Record start and exit separately.** That is what makes an interrupted call
  *unknown* rather than assumed, and an unknown call is resolved by looking at
  whether the worker exists — never by guessing from the log.
- **Invariant 10c.** A wake clears the previous turn's handoff files fail-closed.
  `messages.json` joins `patch.json` and `watch.json` under the same rule.

## The report skill — keep it minimal

The human's instruction is explicit: **minimal and not too constraining.**

Name what the report must make legible — the campaign's reasoning and decisions,
what failed, what progressed, what still awaits a human — and leave the form to
the agent. Invite visualizations and artifacts. Do **not** prescribe a section
list; the failure mode here is a report nobody wants to read, and an
over-specified template is how that happens.

It renders through the existing sandboxed HTML boundary — the same one result
views use. Do not invent an unrestricted campaign document surface.

**A missing or invalid report is a correction, not a verdict.** Hand it back to
the same session with the exact diagnostic under the bounded in-session
correction ladder the Patch path already uses. A correction round never repeats
completed operational work.

## Out of scope

- Real-time streaming (Q8) and worker-to-worker mail (Q9).
- Widening graph Inbox membership for campaign review — the report owns that.
- Exposing the client's verbs on the human `rcp` CLI. It is a separate executable
  so a human cannot accidentally act as an agent, and so the remote host needs no
  RCP installation.
- Team spaces. A campaign has one human authorizer and that is enough here.

## Still undecided — do not answer these by accident

When the report becomes visible relative to still-finishing child work, and the
report skill's exact package and invocation contract. Raise them rather than
picking silently.

## Done means

S77 (`pytest`) and S78 (`browser`) pass. S78's browser half is not optional — the
budget meter, the campaign row, and the report opening from it are the promise.
Serve the app and drive it; check `read_console_messages` and
`read_network_requests` alongside `preview_logs`.

Backend `uv run pytest` and `uv run ruff check src tests`; web
`npm --prefix web run build` and `npm --prefix web test`; then `git add -A` and
`uv run pre-commit run --all-files`.

Skill and workflow package folders are non-Python resources, so a new skill
folder must be named in **both** packaging paths — the wheel force-include in
[pyproject.toml](../../pyproject.toml) and the PyInstaller `datas` in
[rcp_backend.spec](../../packaging/rcp_backend.spec). `official_registry()` runs
on project open, so an omitted folder breaks project open in that build only.
