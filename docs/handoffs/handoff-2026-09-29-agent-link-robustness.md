# Agent link robustness

Date: 2026-09-29
Status: slices A and B implemented; A has a sandbox-limited live check.
An xhigh design review ran on 2026-09-29 and its findings are folded in below. The remaining slices await implementation
and the human decisions below.

Implemented: fix A (server-owned chat wake session binding) and fix B
(newline-only SSE and JSONL text readers).
Remaining: A's live acceptance check, fixes C and D, the open decisions marked
**Human decision**, and the tool audit's remaining owners.

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

Settled with the human on 2026-09-29:

1. The current session is the latest authoritative native-session binding of
   this exact chat, with exact chat, graph target, provider, machine, stage,
   and write-scope checks. Active, paused, resumable, detached, or unresolved
   turns defer the wake. Never search back for an older usable session.
2. Refuse a wake when its recorded provider or model differs from that current
   session, with an actionable recorded reason. Never silently change policy.
3. With no session that can be continued, start fresh visibly, recording whether
   the chat had no session yet or why its current session cannot be continued.
   Experiment and Auto-research child wake policies stay unchanged.

Implemented:

- `AgentTaskStoreMixin._bind_chat_stage` selects the latest native-session task
  from metadata during insertion, inside the existing overlap transaction.
  Human turns and ordinary watcher wakes share that lookup. Paused-turn checks
  now run inside this transaction for both; unresolved current turns defer
  without claiming the watchers. History-only, abandoned-recovery, or missing-stage sessions start
  fresh rather than reviving an older binding. Existing launch-time write-scope
  enforcement continues to validate the resolved filesystem scope.
- `chat_session_resolution` receipts record continued/fresh/refused outcomes,
  reason codes, and source task identity. A provider/model/machine mismatch
  consumes the watcher completion into a failed notification task with an
  actionable error; no provider launches for that task.
- Generic wake admission stores `watcher_wake`, preserving the new logical-turn
  handoff clearing rule (invariant 10c). The chat prompt owner selects `wake`
  on a continuing session and records its node, with the compatible master
  pointer or a bootstrapped master. Provider output is pinned to the resolved
  wake session. No transcript participates in resolution (invariant 10d).
- Task API projections expose `current_chat_session_id`. NodeChat and WebMCP
  consume it without searching native-session history or chat text. Artifact
  context keeps exact artifact ownership but follows the current chat profile;
  insertion owns final session selection. Client session hints cannot select a
  different ordinary-chat session. **New session** still creates a new chat id.

Verification covers same-session human/wake/human admission, paused and
interrupted deferral, provider/model refusal reasons, fresh-start reasons,
no older-session fallback, and recorded wake prompts with master pointers,
including the first Work turn after Discuss and missing-master bootstrap.
Focused storage, API, prompt, watcher, and web checks are recorded in the
implementation report. The local acceptance-provider journey remains open:
this execution sandbox refuses creation of the command broker's Unix socket
with `Operation not permitted`. Run the existing acceptance watcher tests and
one disposable real-provider wake from an environment that permits that broker.

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
files. The remote source index writer escapes Unicode; its reader now splits
on newlines too, so it does not depend on that escaping.

Implemented: split on `"\n"` only in every reader above and the remote source
index reader. Writers retain their existing escaping. JSON parsers retain
the existing tolerance for trailing carriage returns. The supervisor backup
receipt reader uses bytes and already preserves Unicode separators.

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
2. Settled 2026-09-29: on a backend restart or desktop update while a detached
   turn still needs its mailbox, the new backend resumes the same mailbox with
   the same credential. Updates do not wait on agent turns. This needs the
   credential persisted with the same protection as other task secrets.

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

Settled 2026-09-29: feature detection with a visible fallback.

- RCP probes `rsync` once per connection, on the desktop and on the execution
  host, and caches the result with that connection. It never probes per call.
- Contract: rsync 3.1.0 or newer, protocol 31 or newer, on both ends. Apple's
  openrsync reports protocol 29 and fails it. A `PATH` search may offer several
  candidates; the first that passes is used, and its path and version are
  recorded.
- When both ends pass, transfers use rsync. Otherwise they use a tar stream
  over plain SSH: a full copy, no delta, no extra dependency.
- The fallback is never silent. RCP records it once per connection as a
  warning, shows a non-blocking hint that says what to install and where, and
  reports the active engine in diagnostics. It never blocks the work.
- Retry after a killed connection (above) applies to both engines.

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

Settled 2026-09-29: the rsync rule above is the general dependency policy, not
an rsync special case. Every external tool RCP runs is listed in one table in
[server and machine operations](../specs/server-and-machine-operations.md):
where it runs, its owning module, its contract, how it is probed, and its
fallback or refusal. A test scans the source for every external command RCP
starts and fails when one is missing from the table. Probes and fallbacks stay
with their owners; there is no universal runtime registry. Each owner states
discovery, existence check, feature contract, and tested compatibility
separately.

## Temporary states

| State | What happens today | Needed |
| --- | --- | --- |
| Sleep during an accepted turn | sleep logic gates automatic launches only (`src/rcp/machine_sleep.py`) | test disconnection across sleep during requests and settlement, including a restart right after wake |
| Desktop update mid-turn | the update stops the backend after confirming active work (`web/src-tauri/src/updates.rs`) | covered by C's decision 2 |
| Provider update mid-turn | no task gate (`src/rcp/server_ops/provider_update.py`) | **Human decision:** defer while an accepted pass runs, or state recovery |
| Old staged code after an update | recorded settlement validates identifiers and decodes with current code | test an old staged broker and journal against a new backend |
| Usage limit mid-turn | session, quota, and credit exhaustion all classify as `session_limit`; Retry then starts clean | separate account availability from resumability; repeated fresh wakes cannot cure an exhausted quota |
