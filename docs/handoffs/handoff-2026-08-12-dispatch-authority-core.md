# Dispatch — the authority core

**Date:** 2026-08-12
**Scenarios:** [S115](../acceptance/S115-beliefs-change-only-through-you.md) and
[S100](../acceptance/S100-permission-is-checked-twice.md), both confirmed by the
human 2026-08-12.
**Design:** the closed action list in
[Identity, permissions, and agent profiles](../design/identity-permissions-and-agent-profiles.md#action-vocabulary),
settled 2026-08-12.

Read [`AGENTS.md`](../../AGENTS.md) first, then both scenarios, then
[`authority.py`](../../src/rcp/core/authority.py) and
[`proposals.py`](../../src/rcp/core/validation/proposals.py).

This is piece 1 of the three-piece program. Piece 2 (graph-condition wake) is
being implemented in parallel; piece 3 (the orchestrator) depends on both and is
not started. You are building the foundation piece 3 lands on — but build only
what these two scenarios promise. Nothing here is speculative groundwork for the
orchestrator.

## What you are building

An agent may do almost anything to a project. It may not quietly change the
questions being asked or the hypotheses being held. It may argue for such a
change, and then it waits for a human.

Two gates enforce this. One before a provider launches, one when a patch lands.

## The thing to understand before you start

**The rule is unenforceable as the code stands, and closing that gap is most of
the work.**

[`_validate_agent_proposal_boundary`](../../src/rcp/core/validation/proposals.py)
refuses every agent Proposal except a single Hypothesis `status` change carrying
an `evidence_edge` cause. Its catch-all refusal says agents may propose only
Hypothesis status transitions.

So an agent asked to reword an existing ResearchQuestion has no legal move: the
direct edit is forbidden by the new rule, and the Proposal is refused by the old
one. Widening what a Proposal may say is the rule's only exit.

## Land these serially, first

Three shared contracts. None of them fans out.

1. **The action table** in `core/authority.py`. One action per patch operation
   (there are 17 in [materialize.py](../../src/rcp/core/materialize.py)), plus
   the named exceptions in the design document. `permits()` grows from its two
   Decision actions into the real predicate.

   **Derive the action; never guess it from operation shape.** Invariant 3 is
   explicit: a direct Decision choice and an ordinary node edit are both one
   `update_nodes` on one node. The patch's `human_action` field is what
   distinguishes them. Guessing silently reroutes the other one.

2. **The widened Proposal vocabulary** in `core/authority.py`,
   `core/validation/proposals.py`, and `agents/schema.py`. A Proposal now carries
   **one intent** spanning the nodes that intent needs, replacing the
   one-operation-one-node limit.

   Intent is **declared and checked against a closed set of shapes**, never
   inferred from how the operations happen to look. A relaxed limit that accepts
   whatever arrives is a bundle smuggled through, and it is the specific cost the
   human accepted when choosing this option — so it is the specific thing you
   must not let happen. The shapes: content change, removal, supersede, merge,
   protected relation change, and the existing status change.

   `evidence_edge` stays required for **status** changes only. Content changes
   carry their reasoning in the existing `GatedCard` prose.

3. **`web/src/types.ts`** for the new Proposal kinds. Route 3 (result views) also
   needs this file. Coordinate or sequence; do not both edit it.

Then fan out.

## Fan-out

| Agent | Files | Owns |
|---|---|---|
| Graph core | `src/rcp/core/validation/`, `src/rcp/core/authority.py` | the protected-type rule, intent validation, apply-time refusal |
| Agent I/O | `src/rcp/agents/schema.py`, `src/rcp/agents/prompts.py` | the agent-facing Proposal schema and the authority contract text |
| Dispatch | `src/rcp/runs/`, `src/rcp/service.py` | the pre-launch gate, the durable task record, the authority binding |
| Web | `web/src/components/AttentionRail.tsx` | rendering and judging every new Proposal kind |
| Tests | `tests/`, `web/tests/` | both scenarios' checks |

## Invariants you must not break

- **Invariant 3 stands unchanged.** Agents assert or propose; humans hold
  authority. This scenario moves the line between assert and propose; it does not
  give any agent an approval path. No agent approves a Proposal, ever.
- **Attaching Evidence stays direct.** It argues for a status change, and that
  status change is already gated one layer down. Gate both and every Seed and
  Refresh routes through the Inbox.
- **Connecting a node created in the same Patch is not restructuring.** The
  agent creates a Hypothesis and wires it up in one turn; that is creation, which
  is direct.
- **A refused patch is not retracted work.** Repository writes and external calls
  that already happened stand, and the interface says so. And the **answer
  survives** — a rejected patch discarding a chat reply is a failure this repo
  has already shipped once; `TaskFailed` carries partial messages for exactly
  this reason.
- **Apply re-checks live state under the append lock.** A dispatch check is not a
  reservation. Never reintroduce an expected-revision pin or a Resume-ancestor
  walk as a substitute.
- **Replay loads no permission records.** Materialization must succeed with every
  profile and identity record deleted. Once a patch was admitted, a later
  permission change cannot reach back through history.
- **Profiles are code constants**, beside `permissions_for()` in
  [config.py](../../src/rcp/config.py). Nothing in the manifest may widen or
  narrow one.
- **One profile.** Build the ordinary profile only. The elevated orchestrator
  profile arrives with S77 and must not be stubbed in ahead of it.
- **Prompt contracts have line caps.** `tests/test_prompts.py` enforces them, and
  the authority contract body you are rewriting renders into those prompts. Check
  the cap before adding prose, and write tight prose rather than one enormous
  unwrapped line to slip under a line count.

## Coordination

`agents/prompts.py` is currently held by the graph-condition wake route, which is
rewriting the watcher contract in the same file and against the same line cap.
**Let that route land before you rewrite the authority contract body.** Every
other file listed above is free.

## What the dispatch gate checks today

Real content, not a placeholder — but a narrower job than it will eventually have.

- The profile permits the **contract** being dispatched. Today "Work has graph
  authority" is a mode branch (invariant 10b); it becomes a permission lookup on
  the same captured per-turn mode. The captured mode remains the authority, and
  a resumed task keeps its original mode.
- The **durable task record exists before execution begins**, carrying its
  authorizer, project, profile, contract, and scope.
- A refusal **spends nothing**: no provider process, no scratch workspace, no
  ledger entry — and it names which action was refused.

Not today: membership, space binding, campaign, budget. S100 says so and lists
what gets added here when team spaces land.

## Out of scope

- Team membership and revocation mid-run.
- The orchestrator, its campaign, its budget, its children, the `orchestrate`
  contract, and the elevated profile.
- Orchestration commands (`dispatch`, `spawn`, `message`, `watch_graph`,
  `reauthorize_campaign`). They are in the design document's second list so the
  vocabulary is closed, not so they get built now.
- The target grammar for node-scoped work. Still unsettled; see the design
  module's own list.

## Done means

Both scenarios pass. S115 is `pytest + browser` — the browser half is not
optional, because the Inbox rendering each new Proposal kind is half the promise.
Serve the app, produce each kind, and confirm you can read and judge it; check
`read_console_messages` and `read_network_requests` alongside `preview_logs`.
S100 is `pytest`.

Backend baseline `uv run pytest` and `uv run ruff check src tests`; web
`npm --prefix web run build` and `npm --prefix web test`; then `git add -A` and
`uv run pre-commit run --all-files`.

Stamp both scenarios and flip their status when they pass.
