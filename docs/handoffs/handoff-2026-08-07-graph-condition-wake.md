# Handoff — graph-condition wake

**Date:** 2026-08-07
**State:** scope and the rulings marked *decided 2026-08-07* are confirmed by the
human. Everything not marked decided is proposed. **No acceptance scenario has
been written or confirmed**, and no code exists.

**Order in the program:** piece 2 of 3, after
[actor identity](handoff-2026-08-07-actor-identity-and-permissions.md) and
before the [orchestrator](handoff-2026-08-07-orchestrator.md). It is
independent of piece 1 and can land in either order.

Read [`AGENTS.md`](../../AGENTS.md), then the blueprint's
[Watch delivery](../research-control-panel-blueprint.md#watch-delivery)
section, then [`watchers.py`](../../src/rcp/watchers.py), then this file.

---

## 1. Why this exists

A watcher today observes **external** state: a `check_command` polled from a
cold login shell, where exit `0` means the work is gone, `1` means still
present, and anything else means the check cannot answer.

There is no way for an agent to say *"wake me when this graph fact becomes
true."* That gap forces coordination through hearsay — an agent telling another
agent that a blocker was resolved — when the resolution is a **canonical graph
event** that RCP already observes exactly.

The orchestrator (piece 3) is graph-driven by definition: it decides what to
dispatch from the state of the graph. Without this it would have to poll.

## 2. Scope guard — no multi-agent in this piece

The waker and the wakee are the **same conversation and episode**. This piece
delivers exactly one new capability: *an Experiment loop or node agent can wait
on a graph fact instead of on a shell command.* It adds no addressing, no
recipient other than the arming conversation, and no cross-agent delivery.

Piece 3 is where a second party enters.

## 3. What already exists and is reused verbatim

`watchers.py` and the delivery path are, in effect, a durable message queue.
All of this is reused without change:

- `WatcherBinding` pinning recipient identity — provider, session, host, stage,
  conversation, control node;
- coalescing of completions into one distinctly attributed wake;
- the atomic claim, where queue creation, episode-budget admission, and the
  notified ledger commit together;
- one wake spending one invocation unit;
- graceful **Stop loop** terminalizing compatible watchers;
- restart durability, and the staged bounded watcher-state file the agent reads
  instead of the watcher database.

## 4. What is new

A second watcher **kind** whose condition is canonical graph state.

### Condition vocabulary — start minimal

Only what the orchestrator and Experiment loops actually need:

- a node reaches one of a named set of **statuses** (e.g. `blk/foo` becomes
  `resolved` or `superseded`);
- a **Proposal** on a named node is resolved (`approved`, `rejected`, or
  `withdrawn`).

Resist adding standing changes, edge predicates, or arbitrary queries until
something asks for them. A closed two-item vocabulary is checkable and
explainable; a query language is neither.

### Evaluation is event-driven, not polled

Do **not** put graph conditions through `WatcherPoller`. Canonical state changes
only at revision boundaries, so evaluate:

- after any patch applies and after a human Sync; and
- once at startup, so a condition satisfied while RCP was down still fires.

This is cheaper and exact, and it avoids inventing a shell command that
introspects the graph.

### Fail closed on degraded replay

External watchers already distinguish *gone* / *present* / *cannot answer*, and
the blueprint is explicit that RCP never infers a degraded watcher is dead. The
graph analogue: **if replay has halted or materialization is degraded, a graph
condition does not fire.** Not-yet, never completion. A condition on a node that
was removed is terminally retired, matching the existing treatment of a watcher
whose Experiment was removed.

## 5. Decided — `watch-graph` is orchestrator-only

**Decided by the human, 2026-08-07.** Experiment loops arm graph conditions
through `watch.json`, as a second item shape alongside external watchers. The
`watch-graph` command on the staged agent client is available to the
orchestrator only.

The reasoning, kept so it is not re-derived: `watch.json` is the loop's single
**exit declaration**, written once per invocation. A non-empty list means
detached work remains; `[]` is legal only when the same Patch records success, a
Proposal, or a Blocker; validation is atomic, so one invalid item arms none; and
a missing, malformed, or unexplained-empty file routes into handoff correction.

Letting a loop arm by command would break two of those at once. Arming would
stop being all-or-none, because a second call failing leaves the first already
armed. And `[]` would stop meaning "nothing pending, so I must be exiting" — a
loop could arm by command and still write `[]`, which is exactly the state the
correction path treats as a failure to declare an exit.

The orchestrator is not bound by that contract: it arms conditions incrementally
as it decides things, and has no single-exit-declaration semantics to preserve.

**One spelling per caller.** Do not make the file shape available to the
orchestrator as an alternative, and do not expose the command to loops "for
convenience."

## 6. Decisions taken 2026-08-07

Settled with the human. Do not relitigate.

| | Ruling | Why |
|---|---|---|
| Does a graph wake spend a budget unit? | **Always — including when the human's own Sync satisfied the condition.** | "Every wake spends" is what makes running out of budget a *termination guarantee*. One free wake and exhaustion stops being provable. |
| Which conditions exist in v1? | **Two: a node reaching a status, and a Proposal being resolved.** | The second is how auto-research waits for the human, which is the main rhythm of the whole feature. A third condition starts becoming a query language. |
| Variant of the existing watcher record, or a separate type? | **Separate type, sharing the delivery binding.** | `check_command`, `log_path`, and `cwd` are all meaningless here. A type with three dead required fields is worse than two types sharing a wake path. |

Because the two kinds are separate types, the loop's `watch.json` carries **two
named lists** rather than one heterogeneous list:

```json
{
  "external": [{ "check_command": "…", "log_path": "…", "cwd": "…" }],
  "graph": [{ "node_id": "blk/foo", "status_in": ["resolved"] }]
}
```

The existing all-or-none validation and the meaning of an empty handoff apply to
the file as a whole: **both lists empty** is the exit declaration that requires a
success, Proposal, or Blocker in the same Patch.

## 7. Still to verify during implementation

**Coalescing across kinds.** If an external watcher and a graph condition
complete together, they should deliver as one wake. Confirm the existing atomic
claim transaction generalizes cleanly rather than assuming it does — this is a
check to run, not a decision to make.

## 8. Acceptance scenario — written, not yet confirmed

[S76 — An agent can wait on the graph](../acceptance/S76-graph-condition-wake.md).

Driver `pytest`, because every assertion is backend truth — arming, firing,
restart recovery, budget spend, and fail-closed behavior. Nothing about this
promise lives in the browser. Per [`AGENTS.md`](../../AGENTS.md) step 0, confirm
it with the human before implementation.

## 9. Do not

- Do not route graph conditions through the shell poller.
- Do not widen the condition vocabulary to arbitrary graph queries.
- Do not let a graph condition fire from a *staged but unsynced* draft. It is a
  condition on canonical state; anything else reintroduces hearsay with extra
  steps.
- Do not add cross-conversation delivery here. That is piece 3, and it needs the
  authority model from piece 1 to be safe.
