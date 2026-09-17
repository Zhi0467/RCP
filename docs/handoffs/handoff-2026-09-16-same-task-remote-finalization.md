# Finalize disconnected remote turns on their original tasks

Date: 2026-09-16
Status: implementation steps 1-2 complete; steps 3-5 remain. PR #162 is draft and
must not be merged or extended. Its journal, process-safety work, failures, and
tests are evidence for this replacement, not a branch to build upon. This
handoff closes when the replacement PR is implemented, its focused and full CI
checks pass, the served and real-SSH loss journeys pass, and PR #162 is closed
as superseded.

## Human journey

A human sends a remote provider turn, closes the laptop, and returns hours
later. The provider continues under its original authority. RCP resumes
observing that same remote pass and finalizes its durable result on the original
task. Losing the connection does not create a task, consume an invocation,
retry the work, or kill the provider.

The boundary is:

`durable provider result -> original task owner -> idempotent finalizer`

Task and episode kinds select the owning finalizer. They do not make recovery a
new task attempt.

## Settled behavior

1. **Transport loss is a no-op to the provider.** The original task remains
   active and says `Waiting for remote result`. There is no collection child,
   continuation, or human Collect action.
2. **Waiting has no timeout.** An unreachable host preserves the original task,
   stage, journal, and process fence indefinitely. RCP does not automatically
   fail, retry, replace, or kill work that may have started.
3. **Reconnect observes the same pass automatically.** A running provider keeps
   waiting. A stopped provider with a complete journal finalizes the original
   task. Reconnect never starts another provider invocation.
4. **Stop remains explicit and immediate.** Keep the existing human Stop with
   process-identity verification. If the host is unreachable, reject Stop
   visibly and require another click after it returns. Never queue a delayed
   kill.
5. **Bad evidence never triggers automatic work.** Once the host proves the
   provider stopped, an incomplete, corrupt, or invalid journal fails the
   original task visibly. Only a human may Retry.
6. **One rule covers every journaled remote provider turn.** Route the recorded
   result to its original owner. Do not build task-versus-episode eligibility
   branches; each owner applies its normal result contract.

## Architecture contract

### Host-owned acceptance

The remote supervisor atomically records the point at which it accepts
responsibility for the initial provider input. This host-written fact, never the
presence or absence of a local SQLite receipt, decides whether work may have
begun.

After host acceptance, RCP must never invoke a replacement automatically. If the
supervisor dies before forwarding the accepted input, the result is incomplete
and requires human Retry. Before acceptance, Retry is safe only after the
reachable host positively proves that no accepted or running pass exists.

This closes PR #162's gap between a successful prompt write and the controller's
later delivery receipt. Missing controller evidence is silence, not proof that
the prompt never left.

### Delivery state is not task identity

Keep the original operation id, authorizer, capability, graph target, stage,
native session, episode invocation, and accounting identity throughout.

Prefer the existing active task status plus explicit phases such as
`awaiting_remote_result` and `finalizing_recorded_result`; do not add a public
status merely to model a lost transport. The representation must block
overlapping turns and stage reuse, survive RCP restart without claiming a local
worker still exists, be discoverable by one reconciler, and settle the original
row exactly once.

Startup interruption must preserve and reconcile tasks with a host-accepted
journaled pass instead of terminally interrupting them. Local-only or unproven
work keeps the current interruption behavior.

### One immutable recorded result

Load and verify the journal once into one bounded value, for example
`RecordedProviderTurn`. It binds the provider/runtime, pass identity, terminal
outcome, labelled events, usage, native session, Patch, and other deliverables.
Digest, identity, and size validation happen at this boundary. Admission,
streaming, and settlement must not each reread the same journal.

The journal is durability evidence only. It grants no graph authority and runs
no validator.

### One finalizer per owner

Extract each owner's post-provider finalizer and call it from both live delivery
and recorded delivery. The owner keeps the policy that genuinely differs:

- answer and transcript publication;
- usage accounting;
- Patch validation and atomic Apply;
- result-view and artifact settlement;
- Experiment watcher and Stop settlement;
- Auto-research actor, mail, lifecycle, and episode reconciliation; and
- the final task verdict.

Recorded delivery must not rerun profile resolution, admission, prompt
assembly, mail or watcher claims, provider launch, or interactive correction.
Invalid output uses the owner's existing rejected or failed outcome and human
repair path.

Every receipt, transcript record, usage row, and idempotency key names the
original operation. There is no equivalent of `collection_source()` or
`collected_task_operation_id()`.

### What step 1 found for step 3

The finalizer itself took the extraction cleanly: Patch validation and Apply
never needed a provider, only correction did. Three couplings sit in staging
rather than in finalization, and step 3 owns them:

- **The validator mailbox outlives its opener.** `_stage_work_turn` starts it
  and `stream_work_agent_events` closes it, so a caller that skips the stream
  owns the close or leaks a live broker credential.
- **Staging would delete the evidence.** `_stage_work_turn` clears stale turn
  handoffs for the continuations that begin new logical work. Run against a
  stage a finished provider wrote, that removes the very deliverables being
  collected. Recorded finalization must never clear.
- **The prompt/finalize split is not clean.** `skill_selection` feeds the
  watcher continuation and `experiment_resources` feeds watcher maintenance, so
  both must be computed for a recorded finalization even though their staging
  writes must not run. Splitting by "prompt inputs" alone would drop them.

### Restartable finalization

RCP may stop after any finalization side effect. Re-entry detects what the
original task already committed and continues without duplicating its answer,
usage, graph Apply, artifacts, mail, watchers, or episode settlement.

Use existing atomic or idempotent owner operations and a small number of durable
phase receipts. Do not introduce a second general workflow engine. A successful
finalizer completes the original task; provider-declared failure or invalid
durable evidence fails that same task.

## Implementation order

1. **Done.** `finalize_work_result` in `src/rcp/runs/tasks/work.py` is the Work
   owner's post-provider finalizer, taking the staged turn rather than the act
   of having launched something. `maximum_corrections` is its only new input:
   zero means take the owner's existing rejection instead of asking a provider
   nobody is listening to. `tests/test_work_agent_io.py` delivers one result to
   two apps that share no graph -- streamed, and read out of a stage a departed
   provider left -- and pins their durable output equal.
2. **Done.** `rcp/agents/remote_turn_fence.py` holds the one piece of protocol
   knowledge that must exist twice -- where a turn ends -- and
   `tests/test_remote_turn_fence.py` compares it to the canonical decoder on the
   protocol corpus. It publishes no verdict.
   `rcp/transport/remote_turn_supervisor.py` journals the pass and writes
   `accepted.json` before the prompt's first byte reaches the provider; it is
   shipped with the fence composed ahead of it by
   `_remote_turn_supervisor_script`. `rcp/runs/recorded_turn.py` binds and
   verifies one journal, then replays it through the runtime object the live
   pipe drives, so the turn is judged once.
3. Add waiting and reconciliation on the original task across live disconnect
   and RCP restart.
4. Route every journaled remote task owner through the same mechanism with one
   small explicit owner table.
5. Remove transitional duplication before review. The finished diff has one
   live/recorded finalization path per owner and no child-collection remnants.

If step 1 cannot produce a clean shared finalizer without recreating PR #162's
branch matrix, stop and report the concrete coupling before adding journal or UI
work.

## Explicit exclusions

- No `collect` task continuation, task row, recursive chain, authorizer, or UI.
- No task-id remapping helpers or per-surface `continuation == "collect"`
  branches.
- No automatic provider correction while disconnected.
- No remote validator and no second answer file.
- No unrelated staged-preview UI fix from PR #162.
- No general task-lifecycle cleanup beyond what this boundary requires.

## Verification contract

1. **Equivalence:** every owner produces the same task result, transcript,
   usage, graph revision, episode state, mail/watchers, and artifacts when a
   provider result arrives live or from its durable record.
2. **Acceptance crash matrix:** stop the controller before host acceptance,
   immediately after acceptance, during execution, after terminal journal
   commit, and at each finalization checkpoint. No case duplicates a provider
   invocation or Apply.
3. **Waiting:** unreachable waits without spending an attempt; running waits;
   complete finalizes; stopped with incomplete or corrupt evidence fails
   visibly and never auto-retries.
4. **Stop:** reachable and identity-matched Stop terminates and confirms;
   unreachable Stop is refused without a pending intent; stale or recycled pid
   cannot be signalled.
5. **Journey:** with disposable data and protocol-shaped provider stubs, sever
   real SSH, let the remote provider finish, restart RCP, and observe one task,
   one answer, and at most one graph Apply. Drive the served UI and inspect its
   network, console, and server logs.

Run focused checks locally under `AGENTS.md`; full suites run in PR CI. Tests
assert behavior and state, not exact prose or routing-table implementation.

## Reference evidence

- Draft PR #162: https://github.com/Zhi0467/RCP/pull/162
- Exact rejected head at design confirmation:
  `dd5f12fbb476200b7a1296161fbef63b366acb4c`
- The useful evidence is concentrated in the PR's journal/process code and its
  journal, lifecycle, conversation, Work, Auto-research, episode, and remote
  process tests. Historical passing tests are candidates to port only when they
  assert this handoff's behavior rather than child-task implementation details.
