# Graph writes pass two core gates

Confirmed by the human 2026-10-11.

## Decision

Every live graph write passes two gates in the core, in this order:

1. **Permission** (`WriteAuthority` in `core/authority.py`). Who is writing,
   and may they take these actions on this project and graph target? It runs
   under the append lock. It checks human membership live, the agent's task
   binding live, and each operation's graph actions against the graph before
   the Patch.
2. **Validation** (`validate_patch`). Is the write legal for the graph? It
   never looks up identities, membership, or tasks. Rules that depend on the
   writer's role and on state staged earlier in the same Patch read one
   explicit `Writer` value that the permission gate returns.

`HistoryManager` requires a write authority. There is no off switch in the
product code. Replay runs validation only.

Reads keep the project-membership check on the routes.

## Also settled

- **All 17 operations stay.** They are the format writers use: agents get them
  as the staged patch schema, and UI Sync builds the same ops. Each operation
  has one registry profile holding its validation, actions, apply, and
  dependencies.
- **"The graph is still well-formed" is a test, not a runtime gate.**
  `tests/test_graph_invariants.py` drives random op sequences through both
  gates and asserts edge endpoints, edge relation types, open Proposal
  targets, and classified node-id fields. A failure means an operation's
  checks are incomplete; the fix goes in that operation.
- **An edge whose relation does not fit its endpoint types is rejected** for
  new writes. It was only flagged before. The invariant test found it; no
  edge in the real project snapshots checked would have been refused. Replay
  is unchanged.

## Why

Permission was split over the HTTP routes, the history manager, and the
validator. Human membership was checked only at the routes, and the history
manager's attribution switch was off by default. One owner per gate makes a
new writer path inherit both checks instead of re-assembling them.

Some permission rules cannot move ahead of staging. For example, an agent may
create a Hypothesis but not change its status in the same Patch, and only
staged state shows the node is new. Those stay in validation and read the
resolved writer role, so there is one staging loop, not two.

## Not live writes

These publication paths keep their own contracts and do not run the live
permission gate: historical restore (backup restore and transfer import),
project identity claims, project home transfer, the scope bootstrap file,
branch metadata, and merge receipts. A no-change merge receipt always runs the
live authority check.
