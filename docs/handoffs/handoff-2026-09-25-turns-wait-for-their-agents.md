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
  - `launch --service` starts a process that outlives its turn. RCP arms a
    service watcher for it, so it lists and cancels like any job, and never
    wakes an agent.
  - Codex uses an RCP hook fence behind a hook guard that refuses a launch
    when any other hook would load (human choice over an app-server-only
    route).
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

Revised after two Codex xhigh design reviews on 2026-09-25. Their findings
are folded in below.

### 1. One rule for when a turn ends

A turn ends when both are true:

- The provider has finished **RCP's own prompt**.
- The provider reports **no unfinished delegated work**.

The rule lives in one small module. Three readers use it: each runtime's
`receive_line` in `rcp/providers.py` and `rcp/agents/codex_app_server.py`,
the host-side `TurnFence` in `rcp/agents/remote_turn_fence.py` (SSH turns),
and recorded replay (`rcp/runs/recorded_turn.py`). The module ships to the
host from its source. Today the host fence repeats both bugs on its own, so
fixing only the local decoder would leave SSH turns broken.

The module stays pure: it takes input identities, outstanding work, a
deadline and returns a verdict. Launchers own clocks and processes; each
task owner keeps its own deliverables.

RCP has no general turn timeout, and this change does not add one. It adds
one bound, the **delegation wait limit** in `limits.py`.

- The clock starts the first time a turn tries to finish with work still
  open: a blocked Codex `Stop`, or a Claude `result` with a task still open.
- The local launcher, or the execution-host supervisor for SSH turns,
  enforces it. The supervisor enforces it while SSH is detached too. The
  hook never has to be called again for the limit to hold.
- On expiry the launcher stops the process group and seals the verdict
  `delegation_unfinished` in the turn's journal before reporting it.
- The owner then retains the patch text in the database before the failure
  is reported, as it already does for a validation failure. The next chat
  turn clearing the mailbox (invariant 10c) therefore loses nothing
  (invariant 9).
- Live and recorded settlement both carry the typed verdict. Recovery keeps
  the exact session, stage, graph target and Stop fence (invariant 10g). It
  never becomes a success or a fresh-session retry.
- Human Stop and pause keep their current meaning.

**Claude** (`_ClaudeStreamTurn`):

- Every Claude launch sets `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`, so
  subagents and background shells run in the foreground. Parallel subagents
  still work: Claude runs several Agent calls from one message concurrently.
- A successful `result` that attributes **only message ids RCP did not
  send** does not end the turn. This fixes failure 1: Claude Code's injected
  notice is replayed as a user message RCP did not send, and its `result`
  names only that message. Every other case keeps today's behaviour. A
  failing `result` still ends the turn. A `result` with no usable ids still
  stops the process (the 2026-09-08 fence).
- First implementation step: capture a real stream of the injected notice to
  confirm its `result` carries that id. Resuming a session whose background
  agent was killed reproduces it locally. If the id is absent, stop and
  bring the design back.
- A skipped notice `result` stays a trace. It never enters the human answer.
- Backstop: the turn tracks `task_started` / `task_notification` ids. A
  `result` with a task still open starts the delegation clock like any other
  open work; it is not diagnostic only.

**Codex, both runtimes:** an RCP-owned hook fence, behind a hook guard.

- **Hook guard.** `--dangerously-bypass-hook-trust` runs every enabled hook
  outside the sandbox. It is the only way to run RCP's hook, because Codex
  reads hook trust only from the user config layer, and RCP ignores that
  layer on purpose. Probed on Codex 0.157.0 with RCP's exact flags: with the
  bypass flag, the account's `~/.codex/hooks.json` runs; hooks in the turn's
  cwd do not load; without the flag, nothing runs, RCP's hook included.
  App-server's `hooks/list` cannot stand in for exec: app-server has no
  `--ignore-user-config`, so it lists different layers.
  - The guard inspects the hook sources exec loads under RCP's flags, as
    files, on the execution host, as the execution account, with the launch's
    own binary, environment and `CODEX_HOME`. Today that list is the
    account's `hooks.json` plus the managed and system layers. Any foreign
    hook refuses the launch with a typed failure that names each file.
  - That source list is qualified per Codex version by an acceptance probe:
    plant a hook in each source (user, project, managed, plugin) and check the
    guard's inventory against what exec actually runs, for both runtimes.
    RCP records the qualified versions. On an unqualified version the launch
    proceeds with the file guard and shows a visible warning on the turn and
    in Settings until the probe is rerun (human, 2026-09-25: Codex updates
    are usually benign; nothing blocks on them).
  - Positive evidence: RCP's own `SessionStart` hook writes a start marker.
    No marker means the fence is not running, and the turn fails closed.
  - The guard ships from its source module. The leftover risk is the moment
    between the check and Codex starting.
- **Fence.** One hook script, shipped from its source module, handles
  `SubagentStart`, `SubagentStop` and `Stop`. Its state is scoped to one
  invocation: a file named by operation id and attempt, created fresh at
  launch, updated atomically under a lock, and holding the set of started
  ids minus stopped ids. Nested children count the same way. Children from
  an earlier invocation of a resumed session are already dead and never
  appear.
- **Location.** Script, state and marker live in an RCP control directory
  outside every task stage: `<data_dir>/turn-control/<operation>/<attempt>`
  locally, and the matching directory under the execution account's RCP root
  remotely. A stage is not safe: local ingestion and older chat stages use
  the stage itself as the writable workspace. Before launch, RCP checks the
  script, the state file and every replaceable parent against the resolved
  writable roots, and refuses on overlap. RCP passes the paths to the hook
  explicitly, never derived from cwd. Hooks run outside the sandbox
  (verified on Linux below), so they can write there, and the agent cannot.
- **Blocking.** `Stop` with an unfinished child prints
  `{"decision":"block","reason":...}`, rendered from the same object the
  script checks. Each block goes into the turn's journal, so replay reaches
  the same verdict without reading the hook's state. The first block starts
  the delegation clock.
- **exec** passes the hooks with `-c hooks.<Event>=...` and the bypass flag.
  Codex prints two `item.completed` items of type `error` about the flag.
  They stay traces.
- **app-server** replaces `_disabled_hooks` with RCP's fence hooks and the
  thread's `bypass_hook_trust` override. The child-thread notifications it
  drops today feed the same backstop receipt as Claude's task events.
- **Linux**, verified 2026-09-25 on the team-server host with RCP's exact
  Work permission profile (Codex 0.152.0, own account): the fence blocked,
  the child ran inside the sandbox's own PID namespace and finished, and the
  turn completed at 55 s. Hooks ran in the host PID namespace and wrote
  outside the writable roots.

**Every Codex and Claude surface** uses this: Discuss, Work, Experiment loop,
Auto-research child Work, Paper, ingestion and graph repair. Their
capabilities do not change.

**Prompt data:** one rendered fact in every contract. Subagents must finish
inside the turn. Only helper and scheduler jobs outlive it. Tests check that
the data is present, never its wording.

### 2. Codex exec retry notices are traces

`CodexProfile.decode_event` and the host fence: an exec `error` event is a
trace. `turn.failed` or a non-zero exit ends the turn. The last `error` text
is kept. If the process exits non-zero without `turn.failed`, that text is
the failure message and feeds the failure-kind classifier (`stale_session`,
`session_limit`). Recorded replay uses the recorded exit code the same way.
Test fixtures come from the outage stream captured on 2026-09-25.

### 3. The helper is always offered

- `resolve_backend` resolves the OS owner whatever `job_manager` says.
- Scheduler readiness and helper readiness are separate, end to end, as two
  fixed slots per machine, `scheduler` and `helper`, each holding its own
  backend identity:
  - probing (`rcp/compute_jobs/probe.py`) takes the route;
  - storage gets a new migration: the key becomes project, machine and route;
    each retained row moves to the slot its recorded backend names; the other
    slot starts unprobed;
  - the probe API and CLI take the route; the CLI exits zero only when that
    route is ready; Settings shows both slots; a changed compute block
    invalidates both.
  A helper launch uses only the helper slot. An unavailable user manager
  never marks a working Slurm route unready, and the reverse.
- `allowed_verbs` includes `launch` wherever a helper owner resolves.
- `execution_instructions` always renders the helper text. Each configured
  job manager adds its own paragraph from one profile per manager. Slurm's
  says: use Slurm first for compute jobs; use the helper for processes that
  are not compute, such as a dashboard or a local server.
- No silent fallback: RCP never reroutes a Slurm submission to the helper.
- Discuss still has no helper (invariant 4).

### 4. Service launches reuse the watcher

- `launch --service --key K --cwd <path> -- <argv...>` uses the same helper,
  owner, keys and receipts as a job launch. Work, Experiment loop and child
  Work may use it, like `launch`.
- The job record stores service intent. RCP arms one watcher per service
  job, from the helper's own `check_command` and `cancel_command`. The watcher
  carries `kind = service` and a unique link to its job. Job and watcher are
  written in one transaction where the stores allow it. Startup
  reconciliation creates any missing service watcher from the job record,
  so a crash between the two cannot hide a running service.
- A service watcher keeps being polled, but it is outside every wake and
  episode rule: no wake claim, no continuation, not counted as pending
  Experiment work or waiting child Work, not adopted, and not retired by
  episode Stop or ending. It shows in the existing job list with the
  existing attributed, once-only Cancel. When the process exits it shows
  as finished, never "Pending delivery".
- The agent hands off nothing, and settlement accepts the running service.
- A service's cwd, code and assets must live outside the task stage, so
  stage retention cannot delete them. The launch refuses a cwd inside the
  stage; the instructions state the rest. Its helper job root is already
  outside stage retention.
- A service survives its turn. It is not restarted if it exits (launchd runs
  with `KeepAlive=false`, systemd without restart).

## Verification

- Python, focused. One test per rule:
  - the captured Claude injected-notice stream, through the local decoder
    and the host fence;
  - the Claude launch env carries the variable;
  - Codex exec retry `error` events, then `turn.failed`; and then a
    non-zero exit with no `turn.failed`;
  - the hook script run directly: start, block, stop, the wait limit;
  - the hook guard refusing a stray user `hooks.json`, and a turn failing
    closed without the start marker;
  - the delegation limit firing with no further hook call, retaining the
    patch text, on a live and a recorded turn;
  - the readiness migration on a copied database holding a ready Slurm probe;
  - split readiness, `allowed_verbs` and instructions on a Slurm machine;
  - a service launch arms a service watcher that never wakes the agent,
    survives episode Stop, and is recreated by reconciliation.
- The per-version hook-source acceptance probe, run on the current Codex.
- Live, local real providers: a Claude turn, and Codex exec and app-server
  turns, that each spawn a 60-second subagent. Each reply arrives after the
  child's result and uses it.
- Live, disposable server on the Linux host, as `rcp` with its Codex
  0.156.1: one Codex Work turn with a subagent under the real sandbox, one
  SSH turn with a subagent, and a service on the Slurm machine that survives
  the turn and stops on Cancel.

## Spec changes on completion

- `docs/specs/providers-and-containment.md`: the turn-end rule, the Claude
  env var, the Codex hook guard and fence, and the delegation wait limit.
- `docs/specs/compute-jobs.md`: replace the mutually exclusive route
  description with the job-manager profile rule, split readiness, and
  service launches.
- `docs/specs/conversations-episodes-and-watchers.md`: the prompt fact about
  what outlives a turn.
