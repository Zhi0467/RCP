# Agent link robustness

Date: 2026-09-29
Status: design only. No code yet. An xhigh design review ran on 2026-09-29 and
its findings are folded in below. The investigation continues in this PR, and
implementation starts here once the human says so.

Implemented: nothing.
Remaining: fixes A–D, the open decisions marked **Human decision**, and the
tool audit's remaining owners.

Settled with the human on 2026-09-29:

- RCP does not manage provider auto-compaction. The provider owns it.
- In the UI, a chat's watcher wake appears in that same chat. That stays.
- A chat watcher wake should continue the chat's native session.
- Other launch types keep their current session behavior.
- No fix is tuned to the machines this evidence came from: no preferred binary
  path, no `PATH` reordering, no host, OS, or provider exception. For each
  external tool, a fix states the contract RCP needs (version, protocol, or
  feature) and checks it programmatically where the tool runs, or removes the
  dependency. A search heuristic may only find candidates. The binary RCP
  uses must pass the check, and which binary ran is recorded.

## Evidence

Read from real provider transcripts, task events, and the execution host's
`sshd` journal, 2026-09-01 to 2026-09-29, on a personal space with an SSH
execution host and on a team server. Host observations are recorded here as
observed; they cannot be checked from the repository.

- A chat's watcher wake started a new native session each time, with the full
  36.5k-character Work contract. Three wakes in 20 minutes each re-explored and
  ended at 90–150k context. The chat's own session never saw what they found.
- One wake failed on a parse error in RCP, not in the agent (fix B).
- The live Patch validator hung in 3 of 5 calls on the SSH host. Each time its
  poller had already stopped, minutes before the call (fix C).
- Apply was rejected in 2 of 4 turns on the SSH host with
  `rsync(PID): error: unexpected end of file`, five times since 2026-09-11.
  One left the graph stale for 7 days, until a later turn redid it (fix D).
- Agents wrote `patch.json` and `watch.json` correctly in every turn. Every
  watcher registered, including on turns whose Apply was rejected, and fired.

### Why connections die

- At the exact second of three Apply failures and one task-start failure,
  `sshd` logged `ssh_dispatch_run_fatal: … message authentication code
  incorrect` for a desktop connection. A packet arrived corrupted, and `sshd`
  closed that connection with every session on it. openrsync then prints
  `unexpected end of file`, reproduced by killing the remote rsync.
- The host logged 178 of these in four weeks, up to 44 a day, all on
  connections from the one desktop. In three of the Apply failures the
  validator had succeeded 20–46 s before, so the link was up. RCP cannot fix a
  network path. It must expect any connection to die at any moment.
- The two validator deaths left no error in the host's journal. They failed on
  the desktop side: an `ssh` call that stalled until RCP timed it out, once a
  few minutes after the desktop woke from sleep.
- 3 of 12 SSH connections opened at the same instant were reset during key
  exchange (`kex_exchange_identification: Connection reset by peer`). That
  fits a server capping concurrent unauthenticated connections, a common
  default on an internet-facing host. It fails fast with exit 255.
- Ruled out: a file vanishing mid-transfer (exit 24, clear message), a killed
  local `ssh` (exit 20), openrsync on a normal pull (15 of 15 clean against a
  real state repository), and a transfer joining a master as `ControlPersist`
  expires (33 of 33 clean). `ControlPersist` only runs after the last client
  detaches, so it cannot cut a running transfer.

## A. Chat watcher wakes continue the chat's session

Today:

- `_generic_watcher_delivery_request` in `src/rcp/api/app.py` builds the wake
  with `session_id=None`, and `start_watcher_notification` in
  `src/rcp/runs/watcher_admission.py` refuses a generic wake that carries one.
  Experiment and Auto-research child wakes take other paths.
- A human turn's session comes from the Web client: `latestNativeSessionId` in
  `web/src/agentTasks.ts` takes the newest chat task with any session, whatever
  its status. So after a fresh wake, the human's next turn continues the wake's
  session. The server already resolves sessions for artifact context
  (`src/rcp/api/tasks.py`) and recovery, and binds the stage when it inserts a
  task (`src/rcp/storage/agent_tasks.py`).
- The chat prompt owner (`src/rcp/runs/chat.py`) classifies with a hardcoded
  `phase="turn"`. With no session it is a session start, so the full contract
  is the right prompt for the launch as built.
- Chat tasks are already serialized: queued, running, and pausing tasks of one
  chat cannot overlap, and a watcher wake defers on overlap. Watcher admission
  does not check paused, resumable turns, which human admission does
  (`src/rcp/runs/chat_admission.py`).
- **New session** in the chat header creates a different chat id. Watchers
  stay with the old chat. That behavior stays.

Target:

- One server rule resolves a chat's current session, extending the stage
  binding that already happens at task insertion. The Web client uses it too.
- Admission resolves and binds the session atomically, with the same exact
  checks as today: chat, graph target, provider, machine, stage, write scope.
  A wake defers behind an active, paused, or unresolved turn.
- A wake stays a new logical turn with the `watcher_wake` cause, which clears
  the previous turn's handoffs (invariant 10c). It is never relabelled as
  Resume, which keeps them.
- The chat prompt owner selects the `wake` node. It sends the delta and the
  master pointer when a compatible master exists, and bootstraps a master when
  none does, as it does today.
- Session lookup reads task metadata only, never chat text (invariant 10d).

Decisions:

1. **Human decision:** which task is the chat's current session. Recommended:
   the latest authoritative session binding, with exact ownership checks. If
   that session needs recovery, the wake waits. It never searches back for an
   older session that looks usable.
2. **Human decision:** the watcher's recorded provider or model differs from the
   chat's current session. Recommended: refuse the wake with an actionable
   message. Never change policy silently or revive an older session.
3. **Human decision:** a chat with no resumable session. Recommended: start
   fresh only when the chat never had a session; otherwise require explicit
   recovery. Episodes keep invariant 10g.

Checks: drive a chat wake end to end and assert the recorded prompt is a delta
with a pointer, on the chat's session. Cover a failed prior turn, a paused
turn, and the first Work turn after Discuss. Then one real-provider wake on
disposable data.

## B. Event and JSONL readers split on newlines only

`str.splitlines()` also splits on `\x85`, `\u2028`, and `\u2029`, which JSON
leaves unescaped inside strings. An agent that prints such text cuts a record's
JSON in half.

Affected readers:

- `_event_from_sse` in `src/rcp/background.py`, which every background task
  reads its live and recorded events through. This failed a wake.
- Chat transcript append and deduplication in `src/rcp/runs/chat.py`, whose
  writer leaves Unicode unescaped. A later append can fail.
- The chat projection in `src/rcp/service.py`, which returns no transcript on a
  malformed fragment.
- The steering transcript reader in `src/rcp/runs/steering.py`.
- Provider probe events and the Claude skill inventory in
  `src/rcp/providers/claude/profile.py` and `src/rcp/providers/codex/profile.py`.

Already safe: the launcher's byte framing, recorded turns, and remote journal
files. The remote source index uses `splitlines()` but its writer escapes
Unicode; fix it anyway so the reader does not depend on that.

Fix: split on `"\n"` only, in every reader above.

Checks: records holding each of the three characters survive live and recorded
events, transcript append, read, and steering, with identical results.

## C. The command mailbox survives a blip and a detached turn

A command call travels from the agent's client, to the staged broker beside
the provider on the execution host, to a request file in the stage, to RCP's
poller, which lists the stage over SSH. The mailbox carries validation and
compute commands.

Three things lose calls today:

1. `serve_command_mailbox` in `src/rcp/agents/command_mailbox.py` exits on the
   first failed listing and expires the turn's credential, which serves exactly
   one turn.
2. When the stream drops after the host accepted the turn
   (`remote_result_pending`), the Work turn stops and cleans up its mailbox.
   Its worker's event loop then ends, and recorded settlement later runs in a
   new execution. In one case the agent worked 27 minutes with no poller.
3. A request is marked seen before it is read, handled, and answered. A read
   failure answers `unavailable`. A failure while writing the response ends the
   loop after the handler may already have acted.

The staged broker survives a lost link: the remote turn supervisor ignores
`SIGHUP` and treats uplink loss as detachment, and the broker lives while its
provider runs. #225 caps each client wait at 90 s and tells the agent to repeat
the same call. Neither brings the poller back.

The poller lists a remote stage with a new `ssh` process and a remote `python3`
start, then waits `PATCH_SELF_CHECK_POLL_SECONDS` (0.2 s), the value used for
local stages. That is thousands of SSH calls per turn, and one failure ends it.

Target:

- One named owner holds the mailbox, credential, command budget, and cancel
  control from launch until settlement, across detachment. Ownership passes
  explicitly. Before settlement it stops admitting requests and drains the
  ones in flight.
- Each step has its own retry boundary: listing, reading, handling, and
  writing the response. A completed response survives a failed write and is
  written again. Command idempotency keys keep their meaning.
- Temporary failures are classified and retried with bounded backoff. A
  permanent failure or Stop ends the mailbox explicitly. An unreachable host
  never implies a dead provider.
- A remote stage gets its own poll interval, or a design that does not poll.
- Bounds live in `limits.py`.

Decisions:

1. **Human decision:** how long an outage lasts before the mailbox gives up,
   and what the agent sees then. Recommended: back off while the same accepted
   remote pass is unresolved, then answer every call with a stated permanent
   reason.
2. **Human decision:** backend restart or desktop update while a detached turn
   still needs its mailbox. Recommended: the update waits for accepted passes,
   or the new backend resumes the same mailbox with the same credential.

Checks: one failed listing and then a request is answered; a request is
answered after `remote_result_pending`; a lost response after a successful side
effect is replayed, not repeated.

## D. Apply survives a killed connection

Today:

- The state pull (`_sync_remote_tree`), ordinary publication (`_publish`), and
  committed-history publication (`_publish_committed_history`) in
  `src/rcp/transport/state.py` shell out to `rsync` on the shared SSH master.
- Committed-history publication already retries materialization when the
  commit is present, and lost lock ownership triggers commit reconciliation
  (`src/rcp/history/manager.py`). A failed rsync transfer is not retried.
- `apply_work_patch` in `src/rcp/runs/tasks/work_turn_runtime.py` turns
  `StateUnavailable` and `ReplayHalted` into a non-correctable failure. The
  task says "the graph update was rejected", the words used for a Patch the
  rules refused, and it is not repairable.
- Those three transfers set `timeout=120` without catching
  `subprocess.TimeoutExpired`. The background boundary records it as an
  unexpected error and fails the task.
- Transfer stderr is captured whole, then collapsed and cut to 1,600
  characters in the task event (`src/rcp/runs/tasks/work.py`).

Target:

- A killed transfer is retried with the same staged bytes, inside
  `StateWorkspace`. The commit stays present, absent, or unknown, exactly as
  today. A lost acknowledgement never reruns the semantic mutation
  (invariants 6 and 6b). Failed Patch and stage data are retained
  (invariant 9), and `patch.json` stays the only graph-change channel
  (invariant 4b).
- Transport unavailability is its own outcome, not a rejection, and it can be
  repaired or resumed.
- Every failed transfer records bounded raw stderr, the exit code, the phase,
  the partition, and the commit certainty.
- Timeouts are normalized without erasing commit uncertainty.
- The same review covers the other rsync owners: stage inputs
  (`src/rcp/transport/run_stage.py`, which clears its pending inputs on exit
  and must keep the exact snapshot during a retry), backup, restore, kept
  artifacts and result views, and the source index. An accepted provider is
  never relaunched only to redo a transfer.

Decision:

- **Human decision:** how Apply and the state sync move files. Recommended:
  RCP's own shipped scripts over plain SSH, one mechanism on both ends, for
  these small files. Alternatives: bundle a pinned rsync, or state a minimum
  rsync contract and check it wherever rsync runs.

Checks: a transfer killed mid-stream is retried and the commit lands once; a
killed acknowledgement after a confirmed commit leaves one commit; a timeout
reaches a classified outcome.

## Tool audit so far

How the desktop backend finds tools: `repaired_path` in
`web/src-tauri/src/backend.rs` keeps the inherited `PATH`, then appends
guessed folders (`~/.local/bin`, `~/.npm-global/bin`, Homebrew,
`/usr/local/bin`). The first match wins. In the released app observed here,
`/usr/bin` came first, so `rsync` was the system openrsync. Order depends on how
the app was started.

| Tool | Where | How found | What RCP checks |
| --- | --- | --- | --- |
| `rsync` | desktop, and the far end on the execution host | bare name | nothing at runtime; macOS desktop CI runs `/usr/bin/rsync` through a local wrapper, not over SSH |
| `ssh` | desktop | bare name for the backend; `/usr/bin/ssh` for native team tunnels | nothing |
| `python3` | execution host, for shipped remote scripts and the staged broker and client | bare name in the remote shell | used during discovery; no version gate, though the shipped scripts need 3.9 or newer |
| `git` | desktop and execution host | bare name | 2.38 or newer for conversation worktrees; server doctor on Linux servers |
| `ps`, `systemctl`, `systemd-run`, `launchctl` | execution host | bare name | terminal and launch-helper probes |
| provider CLIs | desktop or execution host | configured path, or discovery, then `--version` | readiness probe |

Next: audit each owner in turn, separating discovery, existence checks,
feature contracts, and tested compatibility. Existing checks stay with their
owners; there is no universal tool registry.

## Temporary states

| State | What happens today | Needed |
| --- | --- | --- |
| Sleep during an accepted turn | sleep logic gates automatic launches only (`src/rcp/machine_sleep.py`) | test disconnection across sleep during requests and settlement, including a restart right after wake |
| Desktop update mid-turn | the update stops the backend after confirming active work (`web/src-tauri/src/updates.rs`) | covered by C's decision 2 |
| Provider update mid-turn | no task gate (`src/rcp/server_ops/provider_update.py`) | **Human decision:** defer while an accepted pass runs, or state recovery |
| Old staged code after an update | recorded settlement validates identifiers and decodes with current code | test an old staged broker and journal against a new backend |
| Usage limit mid-turn | session, quota, and credit exhaustion all classify as `session_limit`; Retry then starts clean | separate account availability from resumability; repeated fresh wakes cannot cure an exhausted quota |
