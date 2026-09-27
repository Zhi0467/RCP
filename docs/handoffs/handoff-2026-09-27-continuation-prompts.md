# Continuations send deltas, not contracts

Status on 2026-09-27: design under discussion, revised after a Codex xhigh
design review. Nothing is implemented.

- Settled (human, 2026-09-27):
  - A launch that hands the provider a session id is a continuation. Any other
    launch is a session start.
  - A session start sends the full master contract, as today.
  - A continuation sends only what is new, inline, plus one pointer to the
    master contract. It never resends the contract and never forces a read.
  - The master is re-staged from a durable copy at every launch, so the file
    exists whenever a continuation could read it (option A).
  - Every launch is one node in a per-owner tree. Each node has one of five
    types, and each type has one prompt construction rule.
  - Prose changes stay small. The change is to the construction path.
- Settled by the review (2026-09-27), see [Review outcomes](#review-outcomes):
  current authority always travels inline; the master gets its own durable
  record; the master key includes owner policy; the episode report never gets a
  master pointer; commit timing stays as it is.
  - Construction is a shared assembler, not a registry (human, 2026-09-27).
    Owners keep choosing their builders and call one `compose(node, overrides,
    master_ref)` that applies the node type's rule.
- Open: [Open questions](#open-questions).
- Closure: the slices land with their spec and decision edits, and this handoff
  is deleted.

## Why

An Experiment episode wakes when its watcher fires. The provider gets a short
envelope telling it to read a staged wake file first. Rendered from
`experiment_loop_wake_message` with placeholder paths, that file is about 27,000
characters. About 1,300 are new: why it woke, and this turn's paths. The rest
repeats the session's launch contract: the graph rules (15,600), watcher handoff
rules, Experiment graph authority, failure rules, and reply style. Episodes then
open each wake with "I'll read the current contract…".

The repetition exists so that a session that compacted still has its rules.
The session already holds the contract from its start. It needs a reliable way
back to it, not a fresh copy every turn.

Ordinary Discuss and eligible Work turns already work this way: one staged
master context, and each later turn inline with a delta and a master pointer
(`_prepare_chat_prompt_state` in `src/rcp/runs/chat.py`, `_chat_turn_prompt` in
`src/rcp/agents/prompts.py`). Every other continuation stages a file and forces
a read.

## The principle

The session id the launch hands the provider decides session start versus
continuation. Claude resumes with `--resume`, Codex app-server with
`thread/resume`, and Codex exec with `exec resume`. Invariant 10g never falls
back to a fresh session silently.

A session id does not prove that the session holds a master RCP can find. A
continuation whose session has no durable master record (a session from before
this change, or an owner that never recorded one) bootstraps explicitly: it
stages a freshly rendered master and says so. It never claims "the same
contract you were given".

Sessionless launches are session starts. That includes handoff, switching
provider, a clean Auto-research orchestrator Retry, a progress handoff in
ingestion, and a conversation watcher notification that starts fresh Work.

## Node types

| Type | Prompt rule |
|---|---|
| session start | Record and stage the master. The prompt tells the agent to open and retain it. |
| human turn | Inline: human text, invoked skills, attachments, current overrides, delta, master pointer. |
| wake | Inline: the trigger, what RCP accepted last turn, current overrides, delta, master pointer. |
| recovery | Inline: reason, diagnostics path, current overrides, delta, master pointer. |
| correction | Inline: diagnostics, the owner's exact restriction, current overrides, master pointer. |

**Current overrides** are everything specific to this attempt: paths (graph,
schema, Patch, watch, artifacts, loop control, watcher state), validator and
command credentials and their prefixes, write scope, execution instructions,
selected and invoked skills, attachments, result-view instructions, context
replacement, and each correction's restriction. They always travel inline and
take precedence over the master. A master pointer never restores an expired
command or path.

The master pointer is one rendered sentence:

> Master contract: `<path>`. This is the same contract you were given at the
> start of this session. Read it only after a compaction, or if you have lost
> track of the graph rules or your authority. Current instructions in this
> message take precedence over it.

When the master changes inside a live session, the continuation instead tells
the agent to open the new master and that it replaces the earlier one. That is
the existing chat re-bootstrap.

**The episode report is the exception.** It reuses the operational session but
may read only its frozen inputs and write one HTML file
(`src/rcp/agents/episode_report_prompt.py`). Its payload carries no master
pointer, states that the operational instructions no longer apply, and is never
re-bootstrapped into an operational contract.

The report's restriction is the newest instruction the session holds. So the
first operational continuation after a report attempt on that session (Add N
turns, or an Auto-research lifecycle turn after reauthorization) is a forced
bootstrap: it says the report instructions have ended and tells the agent to
open the operational master again. RCP knows this from the session's last
launch being a report attempt.

## Trees

Each owner below launches only these nodes. Names are today's builders, which
stay the owners of their content. Discuss and Work are separate owners even
though both are chat task kinds.

- **Discuss**
  - start: first message (Discuss master context)
    - human turn: follow-up
    - recovery: Resume, same-session Retry (`discuss.py`)
  - start: handoff (current Discuss base)
- **Work (ordinary and Auto-research child Work)**
  - start: first message (`chat_master_context`; non-master Work: `work_task_contract`)
    - human turn: follow-up
    - wake: child mail or watcher (`_compose_child_wake_prompt`)
    - recovery: Resume, same-session Retry, graph repair (`continuation_task_contract`)
    - correction: Patch, watch, node watcher maintenance
      (`continuation_task_contract` modes, `experiment_watcher_maintenance_correction_contract`)
  - start: handoff (`work_task_contract` plus retry diagnostics)
- **Experiment loop (main and child Experiments)**
  - start: episode start (`experiment_loop_task_contract`)
    - wake: watcher or graph condition (`experiment_loop_wake_message`, cut to its overrides)
    - human turn: Add N turns. A continuation episode runs invocation 1 on the
      ended episode's session and stage (`start_experiment_continuation`), so it
      is a continuation even though its stored cause is `fresh`.
    - recovery: Resume, Retry, graph repair (`experiment_loop_continuation_contract`, its Patch-correction builder for repair)
    - correction: Patch, joint Patch and watch handoff
  - start: switch provider (full contract plus handoff diagnostics)
- **Episode report (both episode modes)**
  - wake: first report, on the operational session (no master pointer)
    - correction: report fix (HTML only)
- **Auto-research actors**
  - start: orchestrator or worker (`auto_research_{orchestrator,worker}_task_contract`)
    - wake: mail, graph, continuation; lifecycle for the orchestrator only
    - recovery: Resume, same-session Retry (continuation builders)
    - correction: Patch, validate-only commands
  - start: clean orchestrator Retry
- **Ingestion (seed, refresh)**
  - start (`graph_task_contract`)
    - recovery: Resume, same-session Retry
    - correction: Patch, with operational authority revoked
  - start: progress handoff (`retry_handoff_task_contract`)
- **Branch merge**
  - start (`branch_merge_task_contract`)
    - correction: merge Patch fix (`branch_merge_correction_contract`)
    - correction: rebase onto new main, with a replaced context, plan, and
      residue (`branch_merge_rebase_contract`)
- **Paper coach**
  - start: first message (`paper_coach_task_contract`)
    - human turn: follow-up
    - recovery: Resume, same-session Retry
  - start: handoff, a Retry that cannot reuse its checkpoint (retry diagnostics)

## Construction

Owners keep choosing their own builders. Each calls one shared assembler,
`compose(node, overrides, master_ref)`, which applies its node type's rule.
There is no registry of owner profiles and no runtime tree check. Shared parts:

- `classify`: an owner supplies its launch phase at the call boundary (the
  continuation value, the session id it hands the provider, and its own local
  counters such as correction, rebase, or report attempt). `classify` checks
  the session id first, then picks one of the four continuation types. It
  never reads the task kind or surface.
- A session master record: immutable master bytes, their digest, and the
  originating operation and role, bound to provider, host, session, project,
  and graph target. `stage()` restores the file into an admissible stage.
- The master key: a manual master version, the graph rules digest, and each
  owner's stable policy version. A key change renders and records a new master;
  re-staging old bytes under a new key is wrong. Paths, credentials, and graph
  data stay out of the key. An explicit `force_bootstrap` covers graph repair
  without faking a content change.
- One delta function over the values each owner declares stable.
- One section table for the shared prose: the pointer, the re-bootstrap line,
  the delta header, and the report's revocation line.

## Review outcomes

The Codex xhigh review (2026-09-27) found five problems in the first draft.
Each is now reflected above.

1. **Current authority must stay inline.** Corrections are not one policy:
   ingestion revokes operational authority, Work keeps it, Experiment watcher
   correction may repair the joint handoff, merge rebase replaces its context,
   and Auto-research correction is validate-only. Each owner keeps its exact
   restriction inline.
2. **No universal durable master exists today.** `agent_task_contracts` is keyed
   by operation and role, not session. Chat records the master's path and
   values, not its bytes. Paper coach follow-ups use a new stage per turn, and
   the report's session belongs to another owner. Hence the new master record.
   Option A restores a missing master file only inside an admissible stage. A
   whole stage that is gone still fails closed, as today; retention does not
   change.
3. **The first tree missed paths**: Discuss versus Work, ingestion recovery and
   handoff, Experiment graph repair, child Experiments, clean orchestrator
   Retry, and the report as its own owner. A table over `AgentTaskContinuation`
   alone cannot classify merge rebase or report attempts, so owners supply
   their local counters.
4. **The graph rules digest is not enough for the key.** Owner authority and
   watcher and recovery policy live outside it. Commit timing also stays as it
   is: chat commits after a completed reply with its compare-and-swap, and
   Experiment commits through settlement. A launch receipt is not proof that
   the provider received the prompt.
5. **The first draft over-built.** The review recommends dropping the runtime
   registry, the runtime tree check, and five constructor classes, in favor of
   owner-selected builders and one small shared assembler. The human chose that.

## Docs this changes

- `docs/specs/providers-and-containment.md`: "Continuation binding" and "Graph
  rules in task contracts" (continuations repeat the rules block). Both become
  the pointer rule, with resend only on a key change.
- `docs/decisions/2026-09-23-graph-rules-render-from-the-model.md`, section "Why
  continuations repeat rather than replace". Replace it with the pointer rule;
  the digest stays as one part of the master key.
- `docs/specs/conversations-episodes-and-watchers.md`, where it describes wake
  prompts.

## Verification

- Every owner launch that can pass a session id (18 invocations, listed in the
  review) produces the expected node type and carries its current overrides.
  Test launch data and enforced authority, not section wording.
- A continuation prompt contains no master contract text and no graph rules
  block, except on a key change or forced bootstrap.
- An Add N turns continuation episode gets a delta, not the start contract.
  After a report attempt on that session, it is a forced bootstrap that retires
  the report restriction, in both episode modes.
- Report attempts in both episode modes carry no master pointer and keep their
  frozen input set.
- A deleted master file inside a live stage is restored with the recorded
  digest. A deleted whole stage still fails closed. Local and remote.
- A session without a master record bootstraps explicitly.
- A release upgrade that changes owner policy re-bootstraps once, then returns
  to deltas.
- Large UTF-8 prompts pass byte-for-byte through Codex exec, app-server, and
  remote forwarding.
- The acceptance fake provider caches what it learned at session start, parses
  current overrides from the inline prompt, and fails on missing commands
  instead of reading them from the master. The other launch-parsing doubles the
  review lists are updated with it.
- Served journey: one Experiment episode through two watcher wakes on a
  disposable data directory, checking the recorded wake prompts.

## Slices

1. Shared parts: `classify`, the master record and `stage()`, the key, the delta
   function, and the section table. Move Discuss and Work onto them with no
   behavior change.
2. Experiment loop and the episode report.
3. Auto-research actors and child Work.
4. Ingestion, branch merge, and Paper coach (including restaging its session
   master into each per-turn stage).
5. Spec and decision edits land with the slice that changes each behavior.

## Open questions

1. **Storage for session state.** Generalize `chat_session_contexts` or add a
   table for non-chat sessions. Recommendation: add the master record first,
   reuse one delta function, and generalize storage later. A broad migration
   should not block slice 1.
