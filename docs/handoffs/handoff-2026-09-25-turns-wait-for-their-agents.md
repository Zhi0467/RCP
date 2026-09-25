# Turns wait for their own agents, and the helper is always offered

Status on 2026-09-25: design only. Nothing is implemented.

- Implemented: nothing.
- Remains: every change below, on one pull request.
- Settled (human, 2026-09-25):
  - A turn waits for the subagents it started. Subagents never outlive the
    turn.
  - One shared rule decides when a turn ends. Each provider runtime feeds it
    through its own wiring.
  - The launch helper is offered by default wherever an OS process owner
    exists. A job manager such as Slurm adds its own rules to the
    instructions. It never removes the helper. See
    [the decision record](../decisions/2026-09-25-job-managers-add-rules-never-remove-the-helper.md).
  - The Codex exec retry-notice fix ships in the same pull request.
  - The live server needs no hotfix: the 2026-09-25 failures came from an
    OpenAI Codex outage.
  - `launch --service` starts a process that runs until a human cancels it.
    It needs no watcher. Work only.
- Closure: the pull request merges, the checks in
  [Verification](#verification) pass, and the specs carry the new sentences.

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

### 1. One rule for when a turn ends

Shared plumbing, `rcp/agents/launcher.py` and each runtime's `receive_line`:
a turn ends when both are true.

- The provider has finished **RCP's own prompt**.
- The provider reports **no unfinished delegated work**.

The existing turn timeout still caps the wait. Stop and pause keep working as
they do now.

**Claude** (`_ClaudeStreamTurn` in `rcp/providers.py`):

- A `result` that arrives before RCP's prompt has `started` never ends the
  turn. This fixes failure 1: the provider-injected notice finishes first, and
  RCP keeps reading until its own prompt's `result`. The 2026-09-08 fence
  still holds: only RCP-generated command UUIDs extend a process, and a later
  `result` with no usable UUIDs still stops it.
- Every Claude launch sets `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`, so
  subagents and background shells run in the foreground. Parallel subagents
  still work: Claude runs several Agent calls from one message concurrently.
- Backstop: the turn tracks `task_started` / `task_notification` ids. A
  `result` with a task still open records a diagnostic receipt. This should
  never happen once the env var is set.

**Codex, both runtimes:** an RCP-owned hook fence.

- One small hook script, shipped from its source module, handles
  `SubagentStart`, `SubagentStop` and `Stop`. It keeps started and stopped
  agent ids in a file in the turn's scratch.
- `Stop` with an unfinished child prints
  `{"decision":"block","reason":...}`. The reason is rendered from the same
  object the script checks. After a fixed number of blocks it allows the
  stop and records a receipt, so a stuck child cannot hold a turn forever.
- **exec:** pass the hooks with `-c hooks.<Event>=...` and
  `--dangerously-bypass-hook-trust`. That flag runs every enabled hook
  without review, so the launch also empties the account's own hook lists,
  as app-server already does. Exec prints two `item.completed` items of type
  `error` about the flag. They stay traces.
- **app-server:** `_disabled_hooks` becomes "RCP's fence hooks, and nothing
  else". The child-thread notifications the runtime drops today feed the
  same backstop receipt as Claude's task events.
- Linux, verified 2026-09-25 on the team-server host with RCP's exact Work
  permission profile (Codex 0.152.0, own account): the hooks fired, the fence
  blocked, the child ran inside the sandbox's own PID namespace and finished,
  and the turn completed at 55 s. Hooks run **outside** the sandbox, in the
  host PID namespace, and can write outside the writable roots. So the fence
  file lives in the task stage, outside every writable root, where the agent
  cannot rewrite it. The service's Codex 0.156.1 is covered by the local
  blocking probe; the live check below repeats it on the server.

**Prompt data:** one rendered sentence in every Discuss and Work contract.
Subagents must finish inside the turn. Only helper and scheduler jobs outlive
it. Tests check that the data is present, never its wording.

### 2. Codex exec retry notices are traces

`CodexProfile.decode_event` in `rcp/providers.py`: an exec `error` event
becomes a trace. `turn.failed` or a non-zero exit ends the turn. The final
`turn.failed` carries the same message, so the failure-kind classifier
(`stale_session`, `session_limit`) keeps working. Test fixtures come from
the outage stream captured on 2026-09-25.

### 3. The helper is always offered

`rcp/runs/tasks/compute_commands.py` and
`rcp/compute_jobs/backends/__init__.py`:

- `resolve_backend` resolves the OS owner whatever `job_manager` says. The
  systemd user-manager probe runs on Slurm machines too.
- `allowed_verbs` includes `launch` wherever an owner resolves.
- `execution_instructions` always renders the helper text. Each configured
  job manager adds its own paragraph from one profile per manager. Slurm's
  says: use Slurm first for compute jobs; use the helper for processes that
  are not compute, such as a dashboard or a local server.
- The settlement check, human Cancel and watchers are unchanged.
- Discuss still has no helper (invariant 4).

### 4. Service launches

- `launch --service --key K --cwd <path> -- <argv...>` uses the same helper,
  owner, keys and receipts as a job launch.
- Settlement does not require a watcher for a still-running service. The
  launch response returns no watcher object for it.
- The service shows in the project's job list with human Cancel, and runs
  until then. Compute belongs in a job launch or the job manager, and the
  instructions say so.

## Verification

- Python, focused:
  - a captured Claude stream where an injected notice `result` comes before
    RCP's prompt;
  - the Claude launch env carries the variable;
  - Codex exec retry `error` events followed by `turn.failed`;
  - the hook script run directly: start, block, stop, cap;
  - `allowed_verbs` and instructions on a Slurm machine;
  - a still-running service passes settlement without a watcher.
- Live, local real providers: a Claude turn and a Codex exec and app-server
  turn that each spawn a 60-second subagent. Each reply arrives after the
  child's result and uses it.
- Live, disposable server on the Linux host: one Codex Work turn with a
  subagent under the real sandbox, and a helper launch on the Slurm machine
  that survives the turn.
- Keep tests proportional: one test per rule.

## Spec changes on completion

- `docs/specs/providers-and-containment.md`: the turn-end rule, the Claude
  env var and the Codex hook fence.
- `docs/specs/compute-jobs.md`: replace "A selected scheduler does not
  silently fall back to the helper" with the job-manager profile rule.
- `docs/specs/conversations-episodes-and-watchers.md`: the prompt fact about
  what outlives a turn.
