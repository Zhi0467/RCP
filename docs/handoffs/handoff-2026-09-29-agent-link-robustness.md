# Agent link robustness

Date: 2026-09-29
Status: fixes A, B and C and D's state-transfer and Apply-recovery work
implemented; live checks remain. An xhigh design review ran on 2026-09-29 and
its findings are folded in below.

Implemented: fix A (server-owned chat wake session binding), fix B
(newline-only SSE and JSONL text readers), fix C (durable command mailbox
ownership and retry), slice 2a (external dependency map and state-transfer
rsync detection with a visible tar fallback), slice 2b (bounded retry of a
dropped state transfer), and D's Apply recovery (the `unavailable` outcome and
Apply again).
Remaining: A's real-provider wake check, D's remaining checks (a killed
acknowledgement after a confirmed commit leaves one commit; a timeout reaches a
classified outcome) and its other rsync owners, and the tool audit's remaining
owners.

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
- Round-1 review fixes: a wake whose trigger is `watcher` now uses the compact
  chat prompt protocol and advances the chat baseline. A current session the
  provider dropped (`stale_session` or unavailable continuation context, as
  Retry classifies them) starts fresh with its reason code; `session_limit`
  continues, because it also matches account quota (see Temporary states). A
  wake deferred behind a running turn records that reason too. A refused wake is stored as failure kind `session_refused` and
  cannot be retried. A deferred wake writes its reason to the watcher's
  `last_error`; graph-condition watchers do not project it yet. Result-view
  revisions keep their own saved session outside this binding and never
  become the chat's current session. A resumed wake keeps the compact protocol. A human's
  session continues across a model switch; only a wake must match the model.

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
- SSH exit 255, no-verdict transport failures, and timeouts of the mailbox's
  own steps retry with capped exponential backoff and jitter. A handler is
  re-run only when its own SSH call gave no verdict, only for checkpointed
  mailboxes, and at most `COMMAND_MAILBOX_HANDLER_MAX_RETRIES` times; any other
  handler failure, including its own deadline, answers "unavailable" so later
  requests are not blocked. Auto-research answers a transport failure as
  "unavailable" and finishes its command row. Each outage and recovery records one event.
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
  A checkpoint is deleted wherever its task leaves `awaiting_remote_result`
  without a live mailbox, and one unreadable checkpoint refuses only its own
  turn during reattachment.

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
  (`src/rcp/history/manager.py`).
- Slice 2b: each of the three transfers is retried as a whole, up to
  `STATE_TRANSFER_ATTEMPTS` in all with capped backoff (`limits.py`), when the
  stream drops: SSH exit 255, rsync exit 10, 12, 20, 30 or 35, or stderr naming
  a reset, closed or timed-out connection, an unexpected end of file, a MAC
  error, or a failed key exchange. A refused transfer (for example exit 23, or
  127), a protocol mismatch, an authentication or host-key refusal, and a
  transfer that used its whole timeout are not retried. A retry re-sends the same staged paths to the same
  staging folder before any commit command runs, so the commit protocol is
  unchanged. The final failure's stderr lists every attempt's exit code and
  bounded stderr, and each retry logs a warning.
- `apply_work_patch` in `src/rcp/runs/tasks/work_turn_runtime.py` records
  `StateUnavailable` as graph update `unavailable`, which offers Apply again
  (see Target). `ReplayHalted` remains a non-correctable rejection.
- Those three transfers now use a timeout owned by `limits.py` and normalize
  process/timeout failures into the existing state-transfer failure boundary.
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
  repaired or resumed. Implemented: `apply_work_patch` records a transport
  failure as graph update `unavailable` with its commit status (`absent`,
  `present`, or `unknown`). Every owner that calls it does this: Work, graph
  repair, Experiment loop, and Auto-research. `HistoryManager` now raises a
  publication failure whose commit point was never observed as
  `BatchPublishFailed(unknown)`. `ReplayHalted` stays a non-correctable
  rejection. Completion keeps the Patch text for `unavailable` too. An ordinary
  Work turn offers **Apply again**
  (`POST …/apply-graph-update-again`, `src/rcp/runs/tasks/work_apply_again.py`,
  projected as `can_apply_again`). It re-applies the retained text through the
  same source binding, and Apply refreshes canonical state under the run lock
  before the binding check, so a landed commit is recorded, never appended
  twice. That makes it safe for `absent`, `present` and `unknown` alike; a new
  Work turn, with a new source id, is the unsafe path. It is refused when the
  text is gone, while the chat is active, after a later turn in the chat that
  committed or may have (`applied`, or `unavailable` with `present` or
  `unknown`) unless this turn's own binding is already in refreshed history,
  and for Experiment-loop or Auto-research child turns. The chat checks repeat
  under the run lock, so a turn admitted while Apply again waits still refuses
  it. A matching canonical commit is marked present before materialization, so
  a later failure keeps that certainty, and a failure before the binding check
  keeps a stored `present` or `unknown`. The task's graph update is swapped
  before any event or receipt is written. Every Apply-again outcome appends a
  chat receipt; only the latest one offers an action. The task event, chat
  receipt, task status line, and Experiment wake summary name the commit
  status. Rules:
  [graph history](../specs/graph-history-and-transitions.md#human-preview-and-agent-correction).
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
- Retry after a killed connection (above) applies to both engines (slice 2b).

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
integration. Commit reconciliation changes and migration of other rsync owners
remain unimplemented.

Remaining checks: a
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
