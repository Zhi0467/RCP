---
id: S88-node-attached-agent-authority
status: implemented
tier: hermetic
driver: pytest + browser
covered_by: tests/test_watchers.py, tests/test_experiment_loop_agent_io.py,
  tests/test_api.py, tests/test_experiment_stop.py, tests/test_prompts.py,
  web/tests/runProjection.test.mjs, web/tests/runDialog.test.mjs,
  browser 2026-08-08
last_passed: 2026-08-08 — full backend and web suites; real-store-copy schema
  integrity, index, and 13-watcher owner backfill; served CRLP new same-node
  session showed 2 live watchers, and Runs showed 2 current plus 3 collapsed
  stopped watchers with no browser console errors.
invariants: [3, 4, 4b, 8, 10b, 10c, 10d, 10g]
reported_by: human, 2026-08-08
---

# Agent authority attaches to nodes, not conversations

Confirmed by the human on 2026-08-08.

A graph node is the minimum resource boundary for agent authority. Operational
resources owned by that node, such as an Experiment's live control episode and
watchers, do not belong to the conversation that created them. A node chat, a
new session on the same node, or a project conversation may act on the same
node-attached resource when the actor's identity, capability, project scope, and
field-level permission allow that exact operation.

Two things travel together here and must stay separated. **Who may act** on a
node's operational resource is permission, checked against the node. **Who gets
woken** when detached work finishes is not a permission question at all: it
follows from which watcher file the agent wrote. A conversation that suspends
itself writes its own file and wakes itself; an Experiment's watcher file always
wakes that Experiment's loop, whoever wrote it.

Node-level authority does **not** mean every field on a node is agent-editable.
Human-only decisions, standing, approvals, episode-level **Stop loop**, and
other protected operations retain their existing authority boundaries. The
point of the node seam is that permission is checked against the target node,
resource, and operation instead of being inferred from which chat happens to
ask. A future identity and permission system can therefore insert its decision
at one admission boundary without preserving conversation-id exceptions.

This scenario first applies that model to Experiment watcher maintenance. It
does not add general direct graph manipulation, let an agent widen its own
capability, or make a conversation transcript an input to another agent.

## Blueprint change

The blueprint currently freezes a watcher's originating **conversation**,
provider, and execution target as automatic-wake compatibility conditions. That
rule is wrong: it describes provenance as if it were permission. The
implementation must correct the blueprint in place, bump its internal version
and changelog, and update the affected implemented clauses in S41, S73, S83,
and S85. No amendment or retained blueprint snapshot is allowed.

The corrected rule has two halves. An Experiment watcher belongs to its **node
and episode**: the Experiment has at most one live control loop, so its watcher
file wakes that loop no matter which conversation wrote it, and neither the
arming conversation nor its configured provider is an admission or delivery
condition. Every other watcher is a conversation **suspending itself**, and
wakes itself for the same reason a resumed task resumes where it paused.

## UI path

1. Start a bounded loop for an Experiment. Its first invocation arms three
   detached-work observers. Leave two healthy and let one become degraded.
2. Open **Chats**, select that Experiment's existing conversation, and press
   **New session**. In Discuss, inspect the node-attached loop state without
   gaining mutation authority. The watcher count in this new session shows the
   node's live watchers, identical to the count in the loop's own conversation,
   and the session can read each existing check's path and host.
3. Switch the new conversation to **Work** and ask it to repair the observer
   commands. RCP exposes the focused Experiment's current loop resource through
   a bounded, current-state pointer. The Work agent inspects authoritative
   scheduler state, retires the three obsolete observers with reasons, and arms
   corrected replacements for work that still exists.
4. Return to **Runs**. The same episode remains active, its native session and
   invocation count are unchanged, the old watchers remain visible as stopped
   history, and only the corrected replacements are active. No generic watcher
   duplicates appear in the maintenance chat.
5. Repeat from a project Work conversation, which reaches the Experiment by
   writing that Experiment's watcher file. The same permission check admits the
   maintenance even though the conversation is not attached to that node, and
   even when it runs a different provider or executes on a different machine.
   Its answer remains in the initiating project chat, while the replacements
   wake the Experiment's loop. In the same turn have it also write its **own**
   watcher file for a long local job, and confirm that watcher wakes the project
   conversation rather than the loop.
6. Try the same write from Discuss, against a non-Experiment node or a node
   outside the actor's resolved scope, against a stale or stopped episode, and
   with requests to change the invocation ceiling, pinned decisions,
   native-session binding, standing, or **Stop loop**. Include one attempt at an
   Experiment watcher path the conversation was never given. Each refused
   operation changes nothing and reports the exact failed permission or live
   resource precondition. It never silently becomes a self-wake watcher.
7. Press **Stop loop** as the human. Runs retains every stopped Experiment
   watcher in history, while each conversation's watcher count includes only
   `active` and `degraded` records—not `stopped` or `completed` records.

## Node resource admission

- RCP derives the actor identity and captured capability from the durable task;
  client-supplied control fields never grant authority.
- Admission checks the project, target scope, resource kind, requested action,
  live episode identity, and field-level permission. **Nothing else.** The
  arming conversation, the maintenance conversation's provider, and the machine
  the maintenance agent runs on are not admission conditions. Chat id is
  retained only as provenance and reply routing.
- Writing an Experiment's watcher file **is** the targeting. RCP never guesses an
  authority target from prose, and no handoff carries a target-node field.
- The current implementation's Work capability may maintain compatible
  Experiment observers by retiring staged watcher ids, arming replacements,
  and defining immutable groups. It may not request the human's episode-level
  stop, change the invocation ceiling or spent budget, replace the pinned
  provider session or stage, edit the governing Decision bundle, set standing,
  or approve a Proposal.
- One function decides every watcher maintenance request, whatever surface
  raised it. A future permission and identity system extends that one decision
  with a principal and policy result. It does not need a new storage path, a
  chat-specific exception, or a second watcher implementation.

## The file is the wake target

There are two watcher files, and which one the agent writes decides who is woken.
Both keep the S83 and S85 item shapes; neither gains a discriminator field.

- **An Experiment's watcher file** is the one source of that Experiment's
  watchers. The loop's own turns write it, and so does any permitted maintenance
  turn from any conversation. Accepted observers bind to the node's live episode
  and their completion wakes that loop. Writing it is the operation S88 gates.
- **A conversation's own watcher file** is how that conversation suspends itself
  until detached work finishes, and its completion continues that conversation.
  This is S42's existing behavior, and it is available to every node chat and
  project chat, including one focused on an Experiment. It is not node-attached,
  because the thing being resumed is the conversation.

Keeping them separate is also the seam for waking a conversation on a graph
condition later: that is a new trigger for the self-wake file, not a change to
Experiment loop delivery.

Staging is discovery, not enforcement. A conversation permitted to maintain an
Experiment is given that file's path, but Work's tooling is unrestricted, so RCP
checks permission when it ingests the file. A path the agent guessed at is
refused exactly as one it was never given, and the refusal names the failed
permission rather than the missing pointer.

## What the prompts must say

RCP tells the agent where checks will run rather than asking it, and no agent
supplies an episode id, a session id, a provider, or an execution host. Naming
the host landed ahead of this scenario, along with the Slurm correction below;
three prompt gaps remain, and each is a silent failure rather than a visible one.

**The prompt must name the wake target of each file it offers.** Today every
surface has exactly one watcher file, so the target is implicit and no contract
states it. A maintenance turn holds two files whose targets differ, and both
validate, so an agent that writes the wrong one produces a watcher that arms
successfully and then wakes the wrong conversation. Each file pointer in the
contract states what completing it does: the Experiment's file continues that
Experiment's loop, the conversation's own file continues this conversation.

**Watcher state must be staged for any conversation that can see the resource.**
`_watcher_state` in [experiment_loop.py](../../src/rcp/runs/experiment_loop.py)
is built only for Experiment-loop turns, so a maintenance agent in a node or
project chat currently cannot see what it is repairing and would be writing
replacements blind. The payload already carries everything needed — watcher id,
status, `execution_host`, `check_command`, `log_path`, `cwd`, group id and label,
error counts, and origin invocation. The work is staging that same payload for
conversations permitted to see it, filtered to the watchers they may see, not
designing a new one. A maintenance agent reads the current checks before writing
replacements; it never reads the watcher database or asks another conversation
what it armed.

**Correction contracts defer to the original contract rather than restating
shapes.** The Experiment-loop watcher correction points at the original contract
by path instead of repeating the item schema, which is why it stayed correct when
S83 added stop items and S85 added groups. The generic Work correction does
restate "exactly three fields," which is right only because generic Work has no
groups or stops. Any new maintenance correction path follows the first pattern:
name the original contract, never re-describe the item shape.

## Session, budget, and provenance

Watcher maintenance runs in the initiating conversation's own Work session and
writes its answer back to that conversation. It never resumes, replaces, or
merges with the Experiment episode's native provider session.

RCP binds accepted replacement observers to the existing node and episode. A
replacement inherits the **episode's** execution host, because a check command
only answers truthfully on the machine holding the work; the maintenance
conversation's own machine and provider are never copied onto the watcher. When
the replacement later completes, it wakes the node's live loop conversation and
its already-bound native session — not the maintenance chat that armed it.

A conversation's watcher count shows what that conversation can see: on an
Experiment node, the node's live loop watchers regardless of which chat armed
them, identical in every conversation on that node; plus its own self-wake
watchers. The maintenance operation and initiating chat remain visible in Runs as
disposition and creation provenance without owning the watcher.

Because watcher maintenance is a separately human-authorized Work task rather
than a loop invocation, it spends no Experiment invocation, creates or closes no
`ExperimentAttempt`, and does not change the episode's last accepted semantic
handoff. Ordinary Work graph reflection, if any, still follows its existing
separate Patch policy and cannot impersonate an Experiment-loop Patch.

An episode created before a newer watcher schema may be maintained through the
current RCP node-resource contract. Repair does not depend on the original
episode provider having seen later grouping or retirement instructions. If old
state lacks enough durable identity to validate the target episode or observer,
RCP fails closed with an exact diagnostic; it does not fall back to chat
ownership, a fresh provider session, or generic watchers.

## Atomic watcher maintenance

RCP validates every requested retirement, observer, and group against one live
snapshot, runs every new observer check, and commits the complete maintenance
handoff under the Experiment's operation lock. One invalid item changes
nothing. A concurrent watcher claim, human **Stop loop**, or other maintenance
turn has one visible winner; the loser receives current-state diagnostics and
cannot partially retire or duplicate observers.

No agent calls a watcher API, edits SQLite, supplies an episode session id, or
writes canonical `.research` state.

## Slurm observer contract

Every agent-facing example that observes a literal Slurm job queries the
complete active-job id set, then interprets membership. The canonical shape is:

```bash
ids=$(squeue -h -o '%A') || exit 2
grep -Fxq 4471 <<<"$ids"
case $? in
  0) exit 1 ;;
  1) exit 0 ;;
  *) exit 2 ;;
esac
```

The exact check is exercised from a cold login shell. It returns `1` while job
4471 is active, `0` when the successful or failed job has left the active queue,
and `2` only when the scheduler query itself cannot answer.

**Corrected on 2026-08-08, ahead of the rest of this scenario.** The Work prompt
already carried this shape; the Experiment-loop prompt — the surface that
actually submits scheduler jobs — carried a direct `squeue -j <id>` lookup in
both its handoff protocol and its wake example, as did S73's example and the Runs
detail web fixture. All are now the set-membership form, and
`test_experiment_work_contract_explains_the_bound_loop_and_watcher_handoff`
asserts the loop contract contains `grep -Fxq` and no `squeue -h -j`. That test
previously asserted the opposite. No prompt, scenario, or fixture may use
`squeue --jobs=4471`, `squeue -j 4471`, or another direct lookup.

This is a prompt-text promise, checked by reading the shipped examples. The
hermetic tier has no scheduler, so a fixture watcher is degraded with any check
that exits `2`; a missing `squeue` binary would prove nothing about the
semantics being fixed.

## Assert

- `watcher_admission_ignores_conversation_identity`
- `watcher_admission_ignores_provider_and_maintenance_machine`
- `new_same_node_work_session_can_maintain_the_active_experiment_loop`
- `writing_the_experiment_watcher_file_is_the_only_targeting`
- `unstaged_experiment_watcher_path_is_refused_on_permission`
- `discuss_can_read_but_cannot_mutate_node_attached_operational_state`
- `client_fields_cannot_forge_node_resource_authority`
- `watcher_maintenance_is_field_scoped_and_cannot_request_stop_loop`
- `experiment_file_is_the_one_source_of_that_experiments_watchers`
- `watcher_contract_names_the_host_and_directory_checks_run_from`
- `each_watcher_file_pointer_states_what_completing_it_wakes`
- `watcher_state_is_staged_for_every_conversation_that_may_see_it`
- `correction_contracts_defer_to_the_original_item_shape`
- `staged_watcher_state_shows_visible_watchers_with_path_and_host`
- `replacement_watcher_inherits_the_episode_node_and_execution_host`
- `experiment_file_wakes_the_live_loop_whichever_chat_wrote_it`
- `self_wake_file_wakes_its_own_conversation_from_any_chat`
- `one_turn_may_write_both_files_without_crossing_wake_targets`
- `maintenance_reply_and_provenance_stay_with_the_initiating_chat`
- `watcher_maintenance_spends_no_loop_invocation_or_semantic_attempt`
- `pre_schema_episode_watchers_are_maintainable_from_current_live_state`
- `invalid_or_racing_maintenance_commits_nothing`
- `watcher_count_shows_visible_watchers_excluding_stopped_and_completed`
- `slurm_example_distinguishes_job_absence_from_scheduler_failure`
- `one_admission_function_governs_every_watcher_maintenance_path`

## Failure means

An agent can repair a node-attached watcher only from the conversation that
created it; a project Work agent with permission cannot explicitly target the
node; maintenance is refused or silently broken because the maintenance chat
runs a different provider or machine; an agent has to guess which host its check
will run on; a guessed Experiment watcher path succeeds because staging was
treated as the gate; a replacement watcher wakes the maintenance chat instead of
the live loop; a self-wake watcher wakes a loop it never belonged to; a new chat
silently creates duplicate observers beside the Experiment's own; maintenance consumes an automatic loop invocation or swaps the
episode's native session; chat identity overrides node permission; an agent
changes a protected human or episode field; one invalid item partially changes
the observer set; stopped history appears as active work; or the shipped Slurm
example degrades a watcher merely because its job completed and disappeared
from the active queue.
