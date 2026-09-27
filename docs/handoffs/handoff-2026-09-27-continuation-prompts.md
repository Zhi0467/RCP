# Continuations send deltas, not contracts

Status on 2026-09-27: design under discussion. Nothing is implemented.

- Settled (human, 2026-09-27):
  - A launch that resumes a provider session is a continuation. Any other
    launch is a session start. The provider session id decides, nothing else.
  - A session start sends the full master contract, as today.
  - A continuation sends only what is new, inline, plus one pointer to the
    master contract. It never resends the contract and never forces a read.
  - The master contract is re-staged from its durable copy at every launch
    (option A below), so the file exists whenever a continuation could read it.
  - Every launch is one node in a per-task tree. Each node has one of five
    types, and each type has one prompt construction rule.
  - Prose changes stay small. The change is to the construction path.
- Open: the questions in [Open questions](#open-questions).
- Closure: the slices land, the spec and decision edits below land with them,
  and this handoff is deleted.

## Why

An Experiment episode wakes when its watcher fires. Each wake today sends about
27,000 characters. About 1,300 of them are new: why it woke, and this turn's
paths. The rest repeats the session's launch contract: the graph rules (15,600),
watcher handoff rules, Experiment graph authority, failure rules, and reply
style. The agent then opens with "I'll read the current contract…" on every
wake.

The repetition exists so that a session that compacted still has its rules.
That is the wrong fix. The session already holds the contract from its start.
It needs a reliable way back to it, not a fresh copy every turn.

Chat turns already work this way. `_prepare_chat_prompt_state`
(`src/rcp/runs/chat.py`) stages one master context, and `_chat_turn_prompt`
(`src/rcp/agents/prompts.py`) sends each later turn inline with a delta and an
`RCP master context: <path>` line. Only `uses_master_protocol` turns get this:
human or orchestrator Work turns that are not retries. Every other continuation
goes through `_stage_task_contract` and `PromptFactory.launch_prompt`, which
stages the whole contract as a file and tells the agent to read it first.

## The principle

The provider session id decides the node type's family:

- Claude resumes with `--resume <session_id>` (`src/rcp/providers.py`).
- Codex app-server calls `thread/resume` instead of `thread/start`
  (`src/rcp/agents/codex_app_server.py`).
- Invariant 10g never falls back to a fresh session silently. A launch that
  carries a session id therefore runs in a session that holds its master.

`handoff` (retry on another provider, host, or a stale or limited session)
carries no session id. It is a session start. So is switching provider in a
chat, because no session binds.

## Node types

| Type | Launches | Prompt rule |
|---|---|---|
| session start | first chat message, episode start, orchestrator, worker, or child start, merge start, ingestion, coach first message, handoff | Stage the master contract from its durable copy. The prompt tells the agent to open and retain it. |
| human turn | chat or coach follow-up | Inline: human text, invoked skills, attachments, delta, master pointer. |
| wake | watcher, graph condition, mail, lifecycle, Auto-research continuation, episode report | Inline: the trigger, what RCP accepted last turn, this turn's paths, delta, master pointer. |
| recovery | Resume, same-session Retry, graph repair | Inline: reason, diagnostics path, delta, master pointer. |
| correction | Patch, watch, or watcher-upkeep fix; merge fix; rebase | Inline: diagnostics path, the narrowed command authority, master pointer. |

The master pointer is always this sentence, rendered by one section:

> Master contract: `<path>`. This is the same contract you were given at the
> start of this session. Read it only after a compaction, or if you have lost
> track of the graph rules or your authority.

When the master changes inside a live session, the continuation instead says to
open the new master and that it replaces the earlier one. That is the existing
chat re-bootstrap. A master changes when its contract key changes: the master
version or the graph rules digest (a release changed them), or a graph repair,
which the spec says renders the current contract.

## Trees

Each task kind allows only the nodes listed. Owner methods are today's builders.
Each becomes that node's `master()` or `payload()` in the kind's profile.

- **Chat turn (Discuss, Work)**
  - session start: first message. `PromptFactory.chat_master_context`
    - human turn: follow-up. `work_turn_prompt`, `discuss_turn_prompt`
    - recovery: Resume, same-session Retry, graph repair. `continuation_task_contract`
    - correction: Patch or watch fix. `continuation_task_contract` correction modes
  - session start: switch provider or handoff. `chat_master_context` plus `retry_handoff_task_contract`
- **Experiment episode**
  - session start: episode start. `experiment_loop_task_contract`
    - wake: watcher or graph condition. `experiment_loop_wake_message`, cut to trigger and paths
    - wake: episode report. `episode_report._stage_attempt_contract`
    - recovery: Resume, Retry. `experiment_loop_continuation_contract`
    - correction: `experiment_loop_patch_correction_contract`,
      `experiment_loop_watcher_correction_contract`,
      `experiment_watcher_maintenance_correction_contract`
  - session start: switch provider. `experiment_loop_task_contract` plus handoff diagnostics
- **Auto-research**
  - session start: orchestrator or worker. `auto_research_{orchestrator,worker}_task_contract`
    - wake: mail, lifecycle, graph, continuation. `auto_research_*_continuation_contract`
    - recovery: Resume, Retry. same builder, recovery modes
    - correction: Patch fix. `continuation_task_contract(work_patch_correction)`
  - session start: child work. `work_task_contract` plus `_auto_research_child_work_contract`
    - wake: message or watcher. `_compose_child_wake_prompt`
    - recovery: Resume, Retry. `_compose_child_resume_prompt`, `_compose_child_retry_prompt`
- **Branch merge**
  - session start: `branch_merge_task_contract`
    - correction: `branch_merge_correction_contract`, `branch_merge_rebase_contract`
- **Ingestion (seed, refresh)**: session start `graph_task_contract`; correction only.
- **Paper coach**: session start `paper_coach_task_contract`; human turn and
  recovery from `runs/tasks/coach.py`.

## Objects

One new module owns the shared path. Owners keep their policy.

- `LaunchFacts`: the continuation value, the session id the launch hands the
  provider, and the correction round. Built from the task execution and request.
- `classify(facts) -> PromptNode`: reads lifecycle facts only. It never reads
  the task kind, surface, or patch kind (AGENTS.md cross-cutting rules).
- `PromptProfile`: one per task kind, registered in one place. It has `tree`
  (the node types this kind allows; any other is a programming error), `master()`
  (the full contract), `values()` (the stable values a delta is computed from),
  and `payload(node)` (the node's new content).
- `SessionMaster`: `stage()` writes the master from its durable copy to one
  content-addressed path in the session's stage `inputs/`, or reuses an
  identical file. It returns the path and digest. This generalizes
  `_experiment_session_contract_path` and the chat master staging.
- `SessionPromptState`: per native session, what RCP last sent. `baseline()`,
  `delta(values)`, and `commit()` after the provider accepts the prompt. This
  generalizes `chat_session_contexts`.
- Five constructors, one per node type. `build()` returns a `ComposedPrompt`
  (today's `_ComposedWorkPrompt`). Shared prose (pointer, delta header, wake
  header, re-bootstrap line) comes from one `PromptSections` table.

Every composed prompt, inline or not, is still recorded with
`record_agent_task_contract` so recovery and receipts see exactly what the
provider saw.

## Master persistence (option A)

The durable copy in `agent_task_contracts` is the source of truth. At every
launch, `SessionMaster.stage()` writes it into the session's stage at the same
content-addressed path, or reuses the file if it is identical.

A continuation always runs on its session's exact stage (continuation binding
in `docs/specs/providers-and-containment.md`). The agent reads the file only
during a live turn, and a live turn's stage is protected from cleanup. So the
file exists whenever it can be read, including after the 7-day sweep removed an
idle stage. Retention does not change.

Rejected: option B, protecting a stage for as long as any of its tasks can
resume or retry. It keeps failed turns' stages forever and still needs the
durable copy for a remote stage that was lost.

## Docs this changes

- `docs/specs/providers-and-containment.md`: "Continuation binding" (recovery
  repeats graph rules) and "Graph rules in task contracts" (continuations
  repeat the block). Both become: a continuation points to its master, and
  resends only on a changed contract key.
- `docs/decisions/2026-09-23-graph-rules-render-from-the-model.md`, section
  "Why continuations repeat rather than replace". Replace it with the pointer
  rule and why. Keep the digest: it is now the contract key's rules component.
- `docs/specs/conversations-episodes-and-watchers.md`, where it describes wake
  prompts.

## Verification

- A table test over every `AgentTaskContinuation` value and correction round:
  `classify` returns the expected node type, and each profile's `tree` accepts
  exactly its nodes.
- Per node type: a continuation prompt contains no graph rules block and no
  master contract text; it contains the master pointer section and the path.
  Test structure and section ids, never wording.
- Contract key change: a continuation after a rules digest change carries the
  re-bootstrap section and a new master path.
- Persistence: delete a stage's `inputs/` master, launch a continuation, and
  assert the file is back with the recorded digest. Local and remote stage.
- The acceptance suite's fake provider reads the task contract from the launch
  prompt (`_read_launch_contract` in `src/rcp/agents/acceptance.py`). It must
  read inline continuation prompts too. Run both acceptance suites.
- Served journey: one Experiment episode through two watcher wakes on a
  disposable data directory. Check the recorded wake prompts' size and that the
  agent does not reopen the master.

## Slices

1. The shared module, `classify`, `SessionMaster`, the constructors, and the
   `PromptSections` table. Move chat onto it with no behavior change. This
   proves the mechanism on the path that already works.
2. Experiment episode: wakes, recovery, corrections, episode report.
3. Auto-research: orchestrator, worker, child work.
4. Branch merge, ingestion corrections, Paper coach, Work retry.
5. Spec and decision edits land with the slice that changes each behavior.

## Open questions

1. `SessionPromptState` storage: generalize `chat_session_contexts` into one
   session-keyed table (a schema migration), or add a second table for
   non-chat sessions. Recommendation: generalize. Two tables would be two
   delta mechanisms.
2. Graph repair renders the current contract today. Treat it as a recovery
   whose contract key always changes, so it re-bootstraps. Recommendation: yes.
3. Correction rounds narrow command authority (for example, Auto-research
   corrections may only validate). That narrowing is new authority and must be
   inline in the correction payload, never only in the master. Confirm every
   correction owner already states it inline.
4. Codex exec takes the prompt on stdin, and app-server on `turn/start`. Inline
   continuation prompts stay well under today's staged sizes, but confirm neither
   transport has a prompt-size limit that the largest delta could hit.
