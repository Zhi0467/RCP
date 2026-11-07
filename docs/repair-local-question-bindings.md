# Repair local question machine bindings

## Outcome

Extend the existing machine-identity upgrade with a narrow, evidence-based repair
for saved local question origins. An answer already accepted by RCP can continue
its original Experiment session and stage, subject to the existing admission
checks and budget. No runtime binding comparison becomes more permissive.

## Confirmed failure

Machine migration 77 converts a task and Experiment's local stage machine from
null to `backend`. Its question-origin conversion leaves a legacy local null as
null. An answered question then proposes a continuation with a null stage machine
against the Experiment's `backend` binding. The strict wake validator refuses
`stage_machine_id` before claiming the answer or spending an invocation.

The answer remains saved and is projected into the chat, but no follow-up task
exists. With no effective watchers or active task, the Runs projection falls
through to Needs action and Stop/restart. A local in-memory reproduction and
read-only inspection of an affected upgraded record confirmed this sequence.

Migration 77 is already ledgered, so changing only its old implementation cannot
repair existing installations.

## Repair rule

Add the next numbered storage migration after `machine_identity_v1`. Its concrete
owner remains `src/rcp/machine_upgrade.py`; `src/rcp/storage/base.py` registers and
runs it through the existing transactional, once-only migration mechanism.

For a question origin whose stage machine is absent or null, find its original
asking task by the recorded operation ID. Repair it to `backend` only when the
task proves the same project, provider, native session and exact stage root,
and the task's stage machine is `backend`. Require a nonempty saved stage root.
Preserve every other origin field and the question's state, answer, revisions,
receipts and follow-up claim. Do not repair a non-null machine identifier,
including an empty string, and do not guess when the asking task is absent or
any identity check differs. Remote origins remain unchanged.

The repair runs after migration 77 on a fresh old-writer upgrade and on an
already-upgraded installation. Re-running the pure repair is idempotent.
Migration IDs and names already published remain unchanged.

Ordinary startup reconciliation then handles any accepted, unclaimed answer.
Its existing atomic claim and invocation debit remain the only dispatch path.
The migration itself does not launch providers, rebuild history, alter the graph,
change credentials, or touch files outside the selected database.

## Alternatives

The numbered repair is selected because it repairs already-ledgered installations
and proves the stored identity once, inside the existing upgrade transaction.
Editing migration 77 alone misses those installations. Normalizing null in the
runtime wake validator is rejected because it weakens the ongoing saved-binding
check. An operator SQL repair is rejected because it adds a separate mutation
path and leaves ordinary upgrades broken.

## Ownership and scope

| File | Change |
| --- | --- |
| `src/rcp/machine_upgrade.py` | Add the proven-local question-origin repair. |
| `src/rcp/storage/base.py` | Register the next migration in the existing ledger. |
| `tests/test_machine_upgrade.py` | Fresh old-writer and already-ledgered repair, idempotence, and refusal boundaries. |
| `tests/test_experiment_questions.py` | Upgraded accepted answer dispatches once on the original session and stage with fake execution. |
| `tests/server_upgrade_fixture_builder.py` | Add the sanitized local-null question seed, executed against the historical base source. |
| `tests/fixtures/server_upgrade/pre-local-question-origin-v43-1226e25/` | Freeze a migration-77 boundary with a local-null question and matching backend asking task. |
| `tests/server_upgrade_harness.py` and fixture README | Register the new immutable bundle's source and digest and describe its boundary. |
| `tests/test_server_upgrade.py` | Check the new boundary's origin repair and preservation through the existing candidate upgrade checks. |
| `docs/specs/projects-spaces-and-operations.md` | Record the question-origin repair beside the machine identity upgrade. |
| `docs/specs/conversations-episodes-and-watchers.md` | Preserve the question's exact session/stage admission contract and link its compatibility repair. |

Build the new immutable bundle from `pre-machine-identity-v42-811b6e7`, using
exact base source `1226e2578e215bd284a3e31bf68ba891c0f1dd79` and the existing
builder's `--source-fixture` path. Generate only a new sibling; never regenerate
an existing boundary. Use the base release's models and storage APIs to seed
neutral identities, a running non-ended Experiment parent with one paid,
settled asking task and two authorized invocations, its matching backend
session/stage binding, and an episode question with a null local machine.
Keep the frozen question pending so
ordinary startup cannot claim an answer before an upgrade check inspects it.
Record the question and task IDs in the fixture metadata and hash the complete
bundle through the existing registry.

The accepted-answer workflow uses a separate temporary copy of this boundary.
The historical base's storage API answers the question while no service runs;
assert null origin, no follow-up claim and unchanged invocation count, then close
the old store. The candidate opens that exact copy, repairs it, and reconciles
with fake admission/dispatch. This proves the unclaimed answer crosses the
upgrade boundary without an old-service startup consuming it. The installed
upgrade harness's existing settled-chat-follow-up check remains unchanged.

Do not alter the runtime wake fence or manually edit live records. General
undelivered-answer retries, diagnostic presentation and the actor-roster running
label are separate findings; they are not needed to repair this proven mismatch.
The coach picker and artifact-identity investigations have separate owners.

## Verification

1. Add a failing old-writer regression before changing the repair implementation.
   Cover a legacy `stage_host: null` through migration 77 and an already-ledgered
   machine-null question. Derive expected schema versions from the registry where
   an exact migration boundary is not the behavior under test.
2. Check null and absent machine fields with matching provenance; preserve remote,
   unknown, empty-string and mismatched project/provider/session/root records.
   Compare all non-machine origin data and answer/receipt fields before and after.
3. Reconcile the upgraded accepted Experiment answer with fake provider admission.
   Assert one task on the original session/stage, one invocation debit, one answer
   claim, and no further task or debit on repeated reconciliation. The frozen
   pending record separately proves origin repair and full record preservation.
4. Run affected serial Python checks, Ruff, pre-commit/type checks, scoped quality
   and mutation checks, independent code review and owning-spec EGM audit.
   The immutable prior-shape and exact-base gates remain covered in PR CI.

No live migration, episode restart, training job or server deployment is included.
