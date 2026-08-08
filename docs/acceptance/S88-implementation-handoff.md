# S88 implementation handoff

Context for an implementing agent. The promise is
[S88](S88-node-attached-agent-authority.md); read it first — this file records
the reasoning behind it and what is already done, not a second specification.
Where the two disagree, S88 wins.

## The one idea

Today a watcher is owned by the chat that armed it. Storage enforces that
literally: `_experiment_watcher_matches_current` in
[storage.py](../../src/rcp/storage.py) requires `record.chat_id ==
root_request["chat_id"]`, and `_validate_and_apply_agent_watcher_stops` requires
`record.chat_id == binding.chat_id`. So an agent can only repair a watcher from
the conversation that created it.

That is wrong. A watcher belongs to the **node**, not the chat. The human's
framing, which the scenario now follows:

> Which file you write determines which chat to wake up. If you write to an
> Experiment node's watcher file, you always wake the Experiment loop — so it is
> purely a permission question of who gets to write it. Any other node chat or
> project chat writes its own watcher file to pause and wake itself later. That
> is a different mechanism, and it is the seam for a later graph-condition wake.

Two consequences worth keeping in mind while implementing:

- **Wake target is a physical fact, not a policy inference.** It follows from
  which file was written. Do not add a discriminator field, a target-node field,
  or a `kind` parameter to a shared helper to recover it (invariant 10).
- **Permission is the only thing left to decide.** It is checked against
  *(project, node, resource, operation)*. Not against chat id, not against the
  maintenance conversation's provider, not against the machine it runs on.

## Decisions the human made, and why

| Question | Decision | Reason |
|---|---|---|
| Who owns a replacement watcher? | The node's episode | An Experiment has at most one live loop, so the node determines the wake target |
| Does provider/machine matching gate maintenance? | No | The episode owns the provider and session; the watcher carries neither |
| Which host does a check run on? | The episode's, told to the agent by RCP | A check answers a different question on a different machine; the agent must not infer it |
| A new file for maintenance? | No | One Experiment, one live loop, one source of watchers, one file |
| Project chats? | Their own self-wake file | There is no loop to wake, so the conversation is the only meaningful target |
| The blueprint contradicts this | The blueprint is wrong | Correct it in place, bump its version |

The provider question is the one that simplified the design most: it deletes a
matching rule rather than relaxing it. Do not reintroduce it as a "compatibility
check" under another name.

## Already done — do not redo

Landed on 2026-08-08 while reviewing the scenario, verified with `uv run pytest`
(all pass), `npm --prefix web test` (204 pass), `ruff check src tests`, and
`tsc -b`:

1. **Prompts name the execution host.** `_watcher_execution_host()` in
   [prompts.py](../../src/rcp/agents/prompts.py) renders ``host `X` `` or "this
   machine", matching the convention repository pointers already used. Threaded
   through `work_task_contract`, `chat_master_context`,
   `experiment_loop_task_contract`, and `experiment_loop_wake_message` as
   `execution_host`, and passed from `work.py` and `discuss.py`, where
   `execution_host` was already in scope. The watcher contract now says RCP runs
   the check there "whether or not that is where this turn is running."
2. **The Slurm example is set-membership everywhere.** Fixed in the
   Experiment-loop handoff protocol and its wake example, S73's example, and
   `web/tests/experimentRunDetail.test.mjs`.
   `test_experiment_work_contract_explains_the_bound_loop_and_watcher_handoff`
   now asserts the loop contract *contains* `grep -Fxq` and contains no
   `squeue -h -j`; it previously asserted the reverse, which is what let the hole
   survive.

## Where the work actually lands

Read these before planning. The fan-out boundaries in
[AGENTS.md](../../AGENTS.md) apply, and `storage.py` is effectively a shared
contract here — land its change serially before the consumers.

- **[storage.py](../../src/rcp/storage.py)** — `_experiment_watcher_matches_current`
  and `_validate_and_apply_agent_watcher_stops` are where chat identity is
  enforced today. This is the seam S88 calls "one admission function." Note that
  the same predicate is used for automatic-wake selection and for stop
  validation; separate those uses rather than loosening one predicate for both.
- **[runs/experiment_loop.py](../../src/rcp/runs/experiment_loop.py)** —
  `_watcher_state` builds the payload the agent reads. It already carries
  `execution_host`, `check_command`, `log_path`, `cwd`, group id and label,
  status, error counts, and origin invocation. Staging it for non-loop
  conversations is the work; redesigning it is not.
- **[runs/work.py](../../src/rcp/runs/work.py)** — around the `watch_text`
  handling, `WatcherBinding` construction, and the
  `request.patch_kind == "experiment_loop"` branch that currently selects the
  parser. The binding is built from the durable task, which is why client fields
  cannot forge authority today; keep that property.
- **[api/app.py](../../src/rcp/api/app.py)** — `deliver_watcher_group` routes on
  `continuation.patch_kind`, and `_generic_watcher_delivery_request` rejects a
  non-`work` continuation. This is where "the file decides the wake target"
  becomes real.
- **[NodeChat.tsx](../../web/src/components/NodeChat.tsx)** — the watcher count
  filters `chat_id === chatId && status !== "completed"`, so an S83
  agent-stopped watcher still counts as live and the count is chat-scoped rather
  than node-scoped. Both halves change.

## Traps

- **Staging a path is not enforcement.** Work's tooling is unrestricted, so an
  agent can write a path it was never given. Check permission when ingesting the
  file; the refusal must name the failed permission, not the missing pointer.
- **Do not add a `kind`/`surface`/`is_chat` parameter to a shared helper.**
  Invariant 10 is explicit: anything that must know which surface it serves is
  policy and belongs in the caller. Duplicated lines are the correct outcome.
- **Migration is invisible to the test suite.** Every test builds a fresh SQLite
  file. A new watcher column must be added through `_ensure_column` and indexed
  only below it, and verified by opening a copy of a real store. This has already
  broken every start once.
- **`.research/patches/` is append-only** (invariant 1), and materialized files
  are outputs (invariant 2).
- **Do not infer the human action from a patch's shape** (invariant 3). Watcher
  maintenance is not an ordinary edit.
- **A pre-schema episode must stay maintainable.** If durable identity is
  missing, fail closed with an exact diagnostic — never fall back to chat
  ownership, a fresh session, or generic watchers.

## Definition of done

S88's Assert list passing, plus the repo baseline: `uv run pytest`,
`uv run ruff check src tests`, `npm --prefix web run build`,
`npm --prefix web test`, and `uv run pre-commit run --all-files` with everything
staged first (`git add -A`, since the hooks only see tracked files).

S88's driver is `pytest + browser`. The browser half is earned by exactly two
things — the watcher count in Chats, and the Runs view keeping stopped watchers
as history — and nothing else in the scenario needs a browser.

The blueprint correction is part of done, not a follow-up: edit
[the blueprint](../research-control-panel-blueprint.md) in place, bump its
internal version and changelog, and update the affected implemented clauses in
S41, S73, S83, and S85. No amendment file, no snapshot.

## One open risk

The wake target for an Experiment watcher rests entirely on "at most one live
loop per Experiment." If that ever becomes two, the node no longer determines the
target and this design needs a real selector. Worth a sentence in the blueprint
correction so the assumption is recorded where it is load-bearing.
