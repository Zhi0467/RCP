# Backend structural refactor closure

Policy has named owners, but the backend remains one coordinated process, not a
replaceable plugin architecture. `BackgroundAgentTasks` may call Auto-research
admission, Experiment recovery, watcher admission and episode reporting directly;
those owners may use documented engine internals for worker identity and launch
gates. Experiment execution may reuse ordinary Work mechanics.

Do not add registries, controllers, callback buses or facades just to hide these
calls. Shared task-row assembly stays shared unless a concrete collision justifies
splitting it. Recovery dispatch stays behind generic retryability guards because
Experiment preflight can write diagnostics or settle Stop; moving it into HTTP
handlers would duplicate guards or cause effects for ineligible tasks.

`api/app.py` remains the composition root for run dispatch, ordered startup
reconciliation and watcher wiring. Extract another owner only for a measured
maintenance collision, independent lifecycle or concrete testing need, not size.

Current execution and child-route behavior belongs to
[Providers and containment](../specs/providers-and-containment.md).
Persisted request retirement belongs to
[the task lifecycle](../specs/projects-spaces-and-operations.md#durable-agent-task-lifecycle).

**Why keep this decision:** it guards a cross-cutting boundary across HTTP,
engine, episode and storage owners. Repeating it in each spec would obscure
which coupling is intentional.
