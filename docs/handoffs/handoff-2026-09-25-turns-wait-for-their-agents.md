# Turns wait for their own agents, and the helper is always offered

Status on 2026-09-25: the simpler design is implemented; live qualification remains.

- Implemented on this branch: sections 1–5, including recorded retry failures.
  The hook fence and completion tracker are removed. Specs carry the design.
- Remains: the local Claude journey in [Verification](#verification), then
  human review and merge. Local checks pass with disposable data and fake providers.
- Settled (human, 2026-09-25):
  - Turns should not lose subagents, but RCP enforces this only where it is
    cheap: Claude runs subagents in the foreground; Codex is told to wait.
  - No Codex hooks, no delegation wait limit, no service watcher kind.
  - The launch helper is offered by default wherever an OS process owner
    exists. A job manager such as Slurm adds its own rules. See
    [the decision record](../decisions/2026-09-25-job-managers-add-rules-never-remove-the-helper.md).
  - The Codex exec retry-notice fix ships in the same pull request.
  - The live server needs no hotfix: the 2026-09-25 failures came from an
    OpenAI Codex outage.
- Closure: the pull request merges, [Verification](#verification) passes,
  and the specs carry the new sentences.

## What went wrong

Three failures on the team server, 2026-09-24 and 25:

1. **A Claude chat lost its subagents and then a turn.** Claude started two
   background subagents and replied "I'll put everything together when they
   report." RCP stops Claude at its first `result`, which killed both. On the
   next message, Claude Code queued its own notice about the killed agents
   ahead of the human's prompt. It emitted an empty, zero-token `result` for
   that notice. RCP stopped there. The human saw "claude finished without
   answering".
2. **A Codex dashboard died with its turn.** Codex served a dashboard from its
   turn; the next turn found it gone. Codex then moved it to Slurm, with
   `high` QoS and no time limit. The launch helper was not offered, because
   the machine is set to Slurm.
3. **Every Codex turn failed in 5 seconds during the outage.** Codex exec
   reports each reconnect attempt as an `error` event. RCP ends the turn on
   any `error`, so it stopped at "Reconnecting... 2/5".

## Probed provider behaviour

Local probes on 2026-09-25: Claude Code 2.1.282 and Codex CLI 0.156.1. The
team server runs the same Claude version.

| Runtime | Subagent at turn end | What RCP can see |
|---|---|---|
| Claude, default | Killed with the process | `system` events `task_started`, `task_notification`, `background_tasks_changed` |
| Claude, `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` | Runs in the foreground. One `result`, after the child finished | Nothing extra needed |
| Codex exec | Killed when exec exits. The JSON stream does not show the spawn | Hooks fire: `SubagentStart` (with `agent_id`), `SubagentStop`, `Stop` |
| Codex exec + blocking `Stop` hook | Parent waits, child finishes, `SubagentStop` fires, turn completes | Same hooks |
| Codex app-server | Root `turn/completed` arrives while the child still runs. Keeping the connection open keeps the child alive, but the root never sees its result | Child thread status and turns on the same connection; `subAgentActivity` items on the root |

A `nohup … &` from a Codex command died on macOS even with app-server still
running. Codex tears down a command's processes when the command ends. Only
the helper or a scheduler can own a process past that point.

## Design

### 1. Claude subagents run in the foreground

- Every Claude launch, local and remote, sets
  `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`. Subagents and background shells
  then finish inside the turn. Parallel subagents still work: Claude runs
  several Agent calls from one message concurrently.
- A successful `result` whose `origin.kind` is `task-notification`, and that
  names no message id RCP sent, does not end the turn and stays a trace.
  Claude Code emits it on resume after a background agent was killed
  (captured on 2.1.283, fixtures under
  `tests/fixtures/claude_turn_completion/`): `system/task_notification`, then
  a zero-turn, zero-usage `result` with that origin and no
  `user_message_uuid(s)`, before it reads RCP's prompt. Every other result
  keeps today's behaviour, including the 2026-09-08 follow-up fence.
- The same rule applies in `_ClaudeStreamTurn` and in the host `TurnFence`
  for SSH turns.

### 2. Codex exec retry notices are traces

An exec `error` event is a trace. `turn.failed` or a non-zero exit ends the
turn. The last `error` text is kept and becomes the failure message when the
process exits non-zero without `turn.failed`, so the failure-kind classifier
still works. Same in the host `TurnFence` and in recorded replay.

### 3. Codex is told to wait for its subagents

- One rendered fact in every provider contract: subagents must finish inside
  the turn; wait for their results before replying; only helper and scheduler
  jobs outlive a turn. It comes from one constant, and tests check that it is
  present, never its wording.
- The fact covers provider-native subagents only. RCP-managed workers, such
  as Auto-research children, keep their own lifecycle.
- A Codex agent that ignores the instruction still loses its subagents.
  RCP does not detect or hold for this; an app-server diagnostic was
  considered and dropped because it prevents nothing.

### 4. The helper is always offered (implemented)

- `resolve_backend` resolves the OS owner whatever `job_manager` says.
- Scheduler and helper readiness are two fixed slots per machine through
  probing, storage (migration 24 sorts retained probes by backend), API, CLI
  and Settings. A helper launch uses only the helper slot.
- `execution_instructions` always renders the helper text; each job manager
  adds its own paragraph from one profile. Slurm's says: Slurm first for
  compute jobs; the helper for processes that are not compute, such as a
  dashboard or a local server.
- No silent fallback. Discuss still has no helper.

### 5. Long-lived processes use plain `launch`

A dashboard or local server uses the ordinary helper `launch` and hands off
its watcher, like any job. The human stops it with the existing Cancel. The
instructions say so. There is no service launch kind.

## Removed

Built earlier on this branch and removed by the rescope, because together
they were about two thirds of the pull request for a partial guarantee:

- the Codex hook fence, the per-runtime foreign-hook guard, turn-control
  directories, start marker, and hook-trust bypass;
- the delegation wait limit, its launcher and host-supervisor enforcement,
  and journal and replay verdicts;
- holding a Claude or app-server turn open while delegated work is open,
  and the shared completion tracker behind it.

The probes behind these remain in the table above and in Git history.

## Verification

- Python, focused, one test per rule: the captured Claude notice stream
  through the local decoder and the host fence; the Claude launch env
  carries the variable; Codex exec retry `error` events, then `turn.failed`,
  and a non-zero exit with no `turn.failed`; the contract fact is present on
  every provider contract; replay of an exec journal that ends non-zero
  after retry notices; split readiness and instructions on a Slurm machine (done).
- Live, local real providers: a Claude turn whose subagent runs 60 seconds
  replies after the child finishes.
- Live, disposable server on the Linux host: a helper `launch` on the Slurm
  machine survives the turn and stops on Cancel (done 2026-09-26, Codex exec
  Work turn: both readiness slots probed ready; the agent chose the helper
  over Slurm for a local HTTP server; it kept serving after the turn under
  its systemd user unit; Cancel stopped the unit and the watcher completed
  on its next check).

## Updated specs

- `docs/specs/providers-and-containment.md`: the Claude env var, the notice
  rule, Codex retry traces, and the subagent contract fact.
- `docs/specs/compute-jobs.md`: the helper, split readiness, and long-lived
  processes using plain `launch`.
