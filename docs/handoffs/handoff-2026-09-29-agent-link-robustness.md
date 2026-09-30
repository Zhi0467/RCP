# Agent link robustness

Date: 2026-09-29
Status: fixes B and C and slice 2a implemented. An xhigh design review ran
on 2026-09-29 and its findings are folded in below.

Implemented: fix B (newline-only SSE and JSONL text readers), fix C (durable
command mailbox ownership and retry), and slice 2a (external dependency map
and state-transfer rsync detection with a visible tar fallback).
Remaining: fix A, the retry and recovery parts of D, and the tool audit's
remaining owners.

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

Implemented:

- `WorkValidatorMailboxLifecycle` owns a remote mailbox on a dedicated thread,
  including its credential, validation budget, completed responses, process
  control, and cancellation events. Work, Experiment, and child Work retain
  their own validation and command policies. Local mailboxes retain their
  turn-local event loop.
- A `remote_result_pending` handoff detaches that owner from the turn worker;
  ending the worker's event loop does not stop serving commands. Background
  reconciliation retains the owner while waiting for the provider. Once the
  provider exits, recorded settlement stops admission and drains the owner
  before graph or watcher settlement; shutdown can still suspend a draining
  owner.
- Listing, reading, handling, and response writing have separate retry
  boundaries. Requests become handled only after durable response publication.
  Completed responses are checkpointed and rewritten after a lost write without
  repeating the handler. Validation retries reserve one budget unit per request;
  keyed command replay retains its existing durable meaning, including #225's
  consumed-file case.
- SSH exit 255, no-verdict transport failures, and timeouts retry with capped
  exponential backoff and jitter. Each outage and recovery records one event.
  A malformed mailbox or refused credential closes the mailbox with a permanent
  reason; the staged broker and client answer later calls with that reason.
  Stop fences new requests and bounds failed drain attempts. An unreachable host
  is never evidence that the provider exited. Remote polls use their own interval;
  polling, retry, jitter, and Stop bounds live in `limits.py`.
- Private restart checkpoints retain the mailbox id, same token, launch policy,
  budget reservations, completed responses, and terminal reason. They reuse the
  provider credential store's atomic mode-0600 writer under its private,
  backup-excluded directory. No secret enters task projections. Startup restores
  the concrete owner before liveness reconciliation, without staging, clearing
  handoffs, reissuing a credential, or requiring a reachable graph host.

Settled decisions, 2026-09-29:

1. Back off while the same accepted remote pass is unresolved. Permanent failure
   or Stop closes it explicitly; later calls receive the stated permanent reason.
   When Stop coincides with an unreachable host, delivery of a closure marker
   cannot be guaranteed; failed delivery is diagnostic and does not hang settlement.
2. A backend restart or desktop update resumes that same mailbox and credential
   for that one turn. Shutdown suspension preserves the checkpoint; startup does
   not clear or recreate the remote mailbox. Updates do not wait for agent turns.

Verification: regression coverage exercises listing/read/handler/write blips,
response replay after a successful effect, permanent later answers, detached
serving after the original event loop exits, restart with the same credential and
budget, and reattachment before reconciliation. Settlement ordering and private
storage permissions are covered. No real host or human data was used.

Remaining verification: the live remote and packaged-desktop journeys are not
verified in this worktree. Existing integration tests that start an account-home
Unix-socket broker are blocked by the execution sandbox (`Operation not
permitted`); module and acceptance results are recorded in the implementation's
commit plan. Fix C has no remaining design decision.

## D. Apply survives a killed connection

Today:

- The state pull (`_sync_remote_tree`), ordinary publication (`_publish`), and
  committed-history publication (`_publish_committed_history`) in
  `src/rcp/transport/state.py` now select a checked rsync or tar-over-SSH engine
  through `src/rcp/transport/state_transfer.py` (slice 2a). Other rsync owners
  still require rsync and have no fallback.
- Committed-history publication already retries materialization when the
  commit is present, and lost lock ownership triggers commit reconciliation
  (`src/rcp/history/manager.py`). A failed rsync transfer is not retried.
- `apply_work_patch` in `src/rcp/runs/tasks/work_turn_runtime.py` turns
  `StateUnavailable` and `ReplayHalted` into a non-correctable failure. The
  task says "the graph update was rejected", the words used for a Patch the
  rules refused, and it is not repairable.
- Those three transfers now use a timeout owned by `limits.py` and normalize
  process/timeout failures into the existing state-transfer failure boundary.
  Classified resumability and bounded retry remain for the next slice.
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

- RCP probes `rsync` once per execution host per backend process, on the backend
  and execution host, and caches completed contract verdicts in the transfer
  owner. Exit 127 or a protocol mismatch invalidates the cached result and
  triggers a new probe. SSH exit 255, timeouts and process-start errors are
  surfaced as unavailable-host failures without caching or warning; the next
  call probes again.
- Updated human decision, 2026-09-29: this is a feature contract, with no rsync
  release-version floor. Both ends require protocol 29 or newer and `-a`,
  `--delete`, `--exclude`, `-R` over `-e ssh`. RCP passes those flags before
  `--version` and parses GNU rsync and openrsync output. Stock macOS openrsync
  passes. A `PATH` search may offer several candidates; the first that passes
  is used, and its absolute path and version are recorded.
- When both ends pass, transfers use rsync. Only a missing or contract-failing
  rsync selects a tar stream over plain SSH: a full copy, no delta.
- The fallback is never silent. RCP logs it once per execution host per backend
  process and also records it through the task-event path when first selected
  during a running task. The warning names the failing end, observed version,
  and required installation. Existing project readiness reports cached engine,
  selected local path, and versions per machine in `state_transfers`; an
  unprobed host has no cached result. No new UI surface was added.
- Retry after a killed connection (above) must apply to both engines; that retry
  work remains outside slice 2a.

Implemented in slice 2a: PATH-ordered executable candidates with no path or OS
preference; both-end protocol/feature checks; state-only tar pull/push; and
source-module shipment of remote tar code. Pushes retain the same staged paths
and canonical lock-holder commit protocol. Pull verifies the complete archive
and extraction, then updates the existing mirror per entry. New or changed files
publish through temporary siblings and `os.replace`; directories and symlinks are
created before stale entries are deleted. Excluded names remain untouched, and
the mirror root is never swapped. Failed transfer or extraction leaves the prior
tree intact. A failure during application leaves complete old or new files;
repeating the pull converges.

Slice 2a checks cover version parsing, PATH selection, cached warnings, engine
invalidation, uncached transport failures, tar mirroring/excludes, interrupted
application and convergence, staged pushes, and the task-event/readiness
integration. Network retry, commit reconciliation
changes, and migration of other rsync owners remain unimplemented.

Remaining checks: a transfer killed mid-stream is retried and the commit lands once; a
killed acknowledgement after a confirmed commit leaves one commit; a timeout
reaches a classified outcome.

## Tool audit so far

How the desktop backend finds tools: `repaired_path` in
`web/src-tauri/src/backend.rs` keeps the inherited `PATH`, then appends
guessed folders (`~/.local/bin`, `~/.npm-global/bin`, Homebrew,
`/usr/local/bin`). The first match wins. In the released app observed here,
`/usr/bin` came first, so `rsync` was the system openrsync. Order depends on how
the app was started.

Slice 2a replaces the preliminary tool list with the source-derived dependency
map in the specification below. State transfers now inspect every executable
named `rsync` on backend PATH in order and use the first passing absolute path;
the inherited PATH is unchanged. Other rsync call sites still require rsync and
have no fallback. Non-rsync feature gates and fallbacks remain with their owners;
the table explicitly marks missing probes rather than claiming they exist.

Settled 2026-09-29: the rsync rule above is the general dependency policy, not
an rsync special case. Every external tool RCP runs is listed in one table in
[server and machine operations](../specs/server-and-machine-operations.md):
where it runs, its owning module, its contract, how it is probed, and its
fallback or refusal. Slice 2a implements that map and
`tests/test_external_dependencies.py`: an AST scan checks external command names
against the table in both directions, including SSH helper argv and literal
remote commands. Fully dynamic argv and `sys.executable` are excluded from the
static name check. Probes and fallbacks stay with their owners; there is no universal runtime registry. Each owner states
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
