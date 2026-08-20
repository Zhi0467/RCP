# Backend structural refactor closure

**Status:** Active architectural boundary  
**Decided:** 2026-08-20  
**Current behavior:** [`../design.md`](../design.md) and
[`../specs/`](../specs/)

## Decision

The backend structural refactor is closed. It achieved explicit route ownership,
durable task admission and ID-only launch, side-effect-free engine construction,
explicit startup reconciliation, and named task/episode policy owners. The old
work order, pickup handoff, and architecture audit are archived as historical
evidence rather than left as active instructions.

The remaining coupling is accepted deliberately:

- `BackgroundAgentTasks` contains common engine methods, but named calls remain
  between it and Auto-research admission, Experiment recovery, watcher admission,
  and episode reporting.
- Those owner modules may use documented engine internals needed for admission,
  recovery, in-process worker identity, and the launch gate.
- Experiment execution may reuse explicit ordinary-Work runtime mechanics rather
  than pretending to be an independent plugin.

This is navigational modularity: policy has an understandable address, while the
application remains one coordinated process. It is not a replaceable plugin
architecture. Do not add registries, generic controllers, callback buses, or
facades merely to hide honest named calls.

## Rejected follow-on changes

### Auto-research owning all task-row assembly

Moving the remaining Auto-research wake cases out of the shared admission method
would require either splitting the universal task-row assembly, duplicating it,
or adding another generic builder. Measurement showed that the shared row
construction is the dominant common body and has not produced owner collisions.
The extra abstraction would cost more than the residual coupling.

### Moving Resume and Retry dispatch into HTTP handlers

Experiment recovery preflight is not a pure classification. It records a durable
diagnostic and may settle a requested Stop, and it must run only after generic
retryability guards. Moving the choice upward would duplicate those guards or
perform side effects for a task that was never eligible. Experiment graph-repair
retries also deliberately continue through the generic path, so the dependency
would not disappear.

### Further extraction from `api/app.py`

`api/app.py` remains the explicit composition root and owns run dispatch, ordered
startup reconciliation, and watcher runtime wiring. No `RunDispatcher`,
`StartupReconciler`, or `WatcherRuntime` wrapper is introduced now. Revisit only
when a measured maintenance collision, independent lifecycle requirement, or
concrete testing problem justifies another boundary.

## Compatibility decisions

An orchestrator-triggered node or project chat is treated as specialized child
Work only when a durable child-route row proves that ownership. If the route is
missing, execution continues as ordinary Work. This compatibility fallback is
intentional and should not be converted into a new failure without a product
decision.

Persisted task-request compatibility is separate. Every task row is normalized
at the storage decoding boundary through one explicit per-kind retirement
allowlist. The only current migration removes legacy `auto_research.ending`.
Unknown fields are preserved so strict request validation rejects them. Stored
mappings assembled outside the row decoder use the same migration helper.

## Consequences

- A new engineer can locate policy by module without assuming the modules are
  independently replaceable.
- Private-looking cross-module calls that implement this accepted boundary are
  not, by themselves, unfinished refactor work.
- Future structural work requires measured evidence, not file size alone.
- Completed implementation plans must be archived promptly so an agent never
  receives obsolete work as active authority.
