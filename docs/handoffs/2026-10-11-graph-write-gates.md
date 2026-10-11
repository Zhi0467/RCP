# Graph write gates

Status: slices A, C, and D are implemented. Operation profiles own action and
apply dispatch. Validation and replay share a manifest-built context. Core
write authority runs before validation. Slice B remains separate work.

## Goal

Every live semantic Patch admission passes two gates, in this order:

1. **Permission.** Who is writing, and may they do these actions on this
   project and graph target?
2. **Validation.** Is this write legal for the graph? It uses the resolved writer
   role for staged rules, without live identity lookups.

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

Live main and branch admissions call core write authority under the append lock.
Identity admission uses its separate manager path. Human and agent membership
are checked live. Constructors require a write-authority object; there is no
production attribution switch. Replay retains persisted-shape checks only.

Each operation has one registry profile for validation, authoring, actions,
application, and dependencies. Validation uses one manifest-built context.

## Target

### Gate 1: permission (`core/authority.py`)

One entry point, called by `HistoryManager` under the append lock before
validation. It takes the pre-Patch graph, patch, authorizer, and graph target.
It returns the stamped patch and resolved writer role, or a rejection.

- Human writer: the authorizer must be a live project member
  (`is_project_member`), checked here, under the lock. This is new.
- Agent writer: the existing task-binding and live-membership checks.
- Patch kind matches the writer: approval is human, identity is system, and
  other kinds are agent.
- Each op's graph actions are allowed for the writer's profile
  (`operation_actions` + `permits`, moved out of the validator).
- Writer action legality lives here. Shape and staged-state rules stay in validation.

Replay does not run Gate 1. Membership is live state, and history is not
re-judged (invariant 6b).

`require_attribution` is removed. Gate 1 always runs. Tests get a fixture
that bypasses live attribution while retaining permission checks.

### Gate 2: validation (`core/validation/`)

`validate_patch` takes the state, the patch, and one `ValidationContext`.
It also receives the resolved writer role for staged rules. The manager builds
the context from the manifest. Branch-merge prechecks use the same builder.

It keeps: envelope rules (schema generation, revision order, scope), each
op's structural and authoring rules, and patch-level rules.

Exceptions and staging boundaries:

- Replay checks persisted producer/author/kind consistency, never live authority.
- Writer-dependent staged rules stay in Gate 2 with one explicit writer value.
- Proposal simulation uses a synthetic human writer and never runs Gate 1.
- Historical restore, identity claims, home transfer, scope bootstrap, branch
  metadata, and merge receipts retain their publication paths. No-change merge
  receipts always run the live authority gate.

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

**D. Permission gate — implemented.** Permission runs before `validate_patch`.
Human membership is checked live and `require_attribution` is removed.
Focused permission, history, validation, and API checks pass. The broad run still
has sandbox-blocked acceptance cases. The unchanged graph-invariant test also
passes a list where slice C requires `ValidationContext`; its four randomized
cases need that separate integration fix.

## Slice D integration facts

- Registration writes system identity before seating the creator; later human
  approvals check the seated membership.
- Personal registration seats the local owner; legacy migration backfills membership.
- Member-page writes carry the authenticated member's authorizer snapshot.
- Test constructors use one test-only stand-in. Production history-only callers
  use an authority without live resolvers, which refuses human and agent admission.

## Invariants

None are relaxed. The work touches 3 (human authority), 4b (the task stage
is the only agent channel), 6 and 6b (one canonical write path, one
transition per mutation).
