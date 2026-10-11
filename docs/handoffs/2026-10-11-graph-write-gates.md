# Graph write gates

Status: slices A and C are implemented. Operation profiles own action and apply
dispatch; validation and replay share a manifest-built context. Slices B and D
remain separate work; the permission gate has not changed.

## Goal

Every graph write passes two gates, in this order:

1. **Permission.** Who is writing, and may they do these actions on this
   project and graph target?
2. **Validation.** Is this write legal for the graph? It never asks who wrote it.

Both gates live in the core. Neither depends on the HTTP layer.

## Settled

- Writes only. Reads keep the project-membership check on the routes.
- All 17 operations stay. They are the format writers use: agents get them
  as the staged patch schema (`agent_output_schema()`), and UI Sync builds
  the same ops.
- The check "the graph after a write is well-formed" is a test, not a
  runtime gate. It exists to prove the per-operation checks are complete.
- Each operation gets one profile that holds all of its code. This is code
  organisation. It does not change the patch format.

## Today

Every committed write already funnels through
`HistoryManager._prepare_transition_locked`. Replay re-runs validation in
replay mode. The funnel is sound. The gates inside it are mixed:

- Human membership is checked only at the HTTP edge (router dependency or an
  inline check). The core trusts the `authorized_by` it is given.
- Agent permission is checked in the core, under the append lock
  (`require_apply`, live membership).
- `HistoryManager(require_attribution=False)` is the default. Production
  turns it on at three sites in `projects.py`.
- `validate_patch` also does permission work: `_validate_authorship`,
  per-op `permits()`, and the authority parts of `validate_approval_shape`
  and `validate_experiment_loop_authority`.
- `validate_patch` takes one `ValidationContext`; history and branch-merge
  prechecks build it from the committing history manager's manifest. Replay
  carries the same context and updates its scope from the staged graph.
- Each operation has one `OpRule` in the validation registry. It binds
  validation, authoring, actions, application, and dependencies. Missing
  profiles raise during dispatch; historical no-ops have explicit handlers.

## Target

### Gate 1: permission (`core/authority.py`)

One entry point, called by `HistoryManager` under the append lock before
validation. It takes the patch, the actor, and the graph target. It returns
the stamped patch or refuses.

- Human writer: the authorizer must be a live project member
  (`is_project_member`), checked here, under the lock. This is new.
- Agent writer: the existing task-binding and live-membership checks.
- Patch kind matches the writer (approval is human; the rest are agent).
- Each op's graph actions are allowed for the writer's profile
  (`operation_actions` + `permits`, moved out of the validator).
- The authority parts of approval and experiment-loop checks move here.
  Their rule parts stay in validation.

Replay does not run Gate 1. Membership is live state, and history is not
re-judged (invariant 6b).

`require_attribution` is removed. Gate 1 always runs. Tests get a fixture
that supplies a resolver and a membership check.

### Gate 2: validation (`core/validation/`)

`validate_patch` takes the state, the patch, and one `ValidationContext`.
It has no actor input. The manager builds the context once from the
manifest. The branch-merge pre-check uses the same builder.

It keeps: envelope rules (schema generation, revision order, scope), each
op's structural and authoring rules, and patch-level rules.

### Operation profiles

One `OpProfile` per op holds: validate, authoring, graph actions, apply,
and dependencies. The registry is the only dispatch table. `_apply_patch`
dispatches through it, and an unknown op raises. A test asserts every op
in the `GraphOperation` union has a complete profile.

### Graph-invariant test

A test builds random valid patch sequences, sends each through Gate 2 and
apply, and asserts the result is well-formed:

- every edge's source and target exist;
- every node id held in another field points to a live node, or the field
  is listed as history (for example an attempt's decision pin);
- edge types fit the relation.

A failure means an op's checks are incomplete. The fix goes in that op.

## Slices

Each slice updates the specs it changes and adds tests in proportion.

**A. Operation profiles — implemented.** Move code, no behaviour change. Check: the
validation, materialize, and authority test files pass unchanged.

**B. Graph-invariant test.** Add the test. Fix any op it catches.

**C. Validation context — implemented.** One context object and builder. Check: branch
merge and history tests.

**D. Permission gate.** Move permission out of `validate_patch`, add the
human membership check, remove `require_attribution`. Check: permission
tests that attempt each refusal (non-member human, departed agent
authorizer, wrong profile action), plus the Sync, approval, provisioning,
and branch-merge API tests.

## Open checks before slice D

- Project creation: is the creator a project member before the first
  approval patch lands? If not, the new check refuses project setup.
- Personal (desktop) space: confirm the local owner is a member of every
  project it writes.
- An agent on a member's own page writes as that member. Confirm it carries
  that member's `authorized_by`.
- About 210 test sites construct `HistoryManager`. Count how many need the
  new fixture.

## Invariants

None are relaxed. The work touches 3 (human authority), 4b (the task stage
is the only agent channel), 6 and 6b (one canonical write path, one
transition per mutation).
