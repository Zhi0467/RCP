# A finished turn survives a lost link

Date: 2026-09-15
Status: completed on PR #162. Remote journals, provider-free collection,
reachability-gated recovery, graph Apply, and chat catch-up are implemented.
Disposable API, served-browser, restart, correction, and episode checks passed.
Real SSH interruption was qualified with protocol-shaped stubs for both providers;
no paid provider invocation or production data was used for that check.

Settled with the human: a disconnected provider keeps running within its
original authority. Reconnecting while it is alive waits for completion without
spending another attempt. Only a stopped provider without protocol completion is
incomplete. The live pipe remains the control channel; a bounded journal makes
finished output durable. No remote validator, second answer file, or replacement
provider is part of collection.

## The incident, from the record

Two Work turns were sent from a laptop that then slept. All times UTC.

1. `22:09` and `22:19`: Claude and Codex conversation Work turns started on the
   same remote execution host. Both were ordinary chats, not episodes.
2. Both providers edited repositories, ran tests, wrote documentation, and wrote
   `patch.json`. Those changes survived on the execution host. Four validation
   callbacks between `22:38` and `22:55` received no response; both providers
   correctly treated validation as unavailable and continued.
3. At `22:44:47` and `22:58:10`, the providers produced complete final answers
   in their native session logs. RCP received neither answer or Patch. At
   `22:51`, failed transport receipts had already recorded an unreachable host
   and `remote_process_stopped: false`, leaving both stages fenced.
4. At `23:31`, wake resumed the automatic retry timers before the network
   returned. Both attempts failed during admission; collapsed stage-probe
   failures ended the recovery chain at attempt one of three. Claude had exited;
   Codex remained blocked writing to an undrained stdout pipe after completing
   its turn, holding its stage fence for several hours.
5. A separate episode turn at `00:44` suffered the same transport loss and
   recovered because its later attempt reached an awake, connected controller.

The remote process lifecycle already survived the disconnect: detached process
groups, pidfiles, retained stages, and durable start/stop receipts preserved the
work. Delivery of its answer and graph Patch did not survive. Native session
logs later proved both providers had answered; a provider still alive after its
terminal event was not evidence that its research was unfinished.

## Implementation contract

### Reachability owns recovery timing

A timer schedules a probe, not a provider invocation. An unreachable host keeps
the same automatic attempt owed, with a durable waiting receipt. Restart re-arms
that owed attempt. Shutdown and an already-admitted continuation make the wait
stand down, including when either happens while the probe is in flight.

`RemoteRunStage.attach` preserves the distinction already available from
`directory_exists`: an unreachable host raises a typed transport failure; a
missing or unsafe directory remains a different failure. Ordinary admitted
reattempts remain bounded. Retry already preserves an eligible native session;
its defect was the absence of collection, not an unconditional cold restart.

### The pipe controls; the journal preserves

Every remote provider pass writes `<pid_file>.turn/` beneath its existing stage.
It contains raw provider stdout in `events.jsonl`, bounded stderr, an optional
completed `patch.json` snapshot, and atomic `outcome.json`. The receipt binds
provider/runtime identity, protocol completion, and content digests to that exact
pass. Existing provider decoders derive labelled answers and usage from events;
there is no `answer.md` and no duplicate answer authority.

The pipe still carries startup release, broker requests and responses, live
validation, steering acknowledgments, and Stop. The execution-host journal
observes completion and fences later stdin while draining provider stdout
independently of the uplink. Explicit disk and memory ceilings bound that drain;
overflow cannot silently become a complete turn. A completed Codex process must
not remain alive solely because the uplink stopped draining.

### Collection adopts the original work

Collection is a parent/child continuation with no provider launch. It preserves
the original human authorizer, capability, mode, host, stage, session, graph
target, and episode invocation. It reads only passes named by the saved launch receipts and uses existing
decoding and finalization paths. The operational pass supplies the human answer;
later corrections supply deliverables and usage. Incomplete corrections preserve
the answer and retain completed Patch bytes for repair, without a new Apply.

If the host cannot be reached, keep waiting. If the original process is alive,
keep its stage fence and wait for it to finish. Only after process absence is
confirmed can a complete journal be collected or missing protocol completion be
settled as incomplete. A disconnect during execution is not itself incompletion.
Collection does not replace the interactive connection, acknowledge old steering,
resend a message, or invoke automatic provider correction.

A valid collected Patch commits once on the original graph target. A collected
answer without a Patch is an ordinary Work result. A rejected Work Patch retains
its answer and diagnostics and enters existing graph repair. Auto-research must
retain its actual repair eligibility; collection cannot advertise unsupported
repair. A graceful episode Stop remains in force throughout collection and
cannot admit another invocation or reenable watcher delivery.

### No remote validator

The existing agent contract already treats unavailable validation as a valid
outcome, and both providers in the incident used it correctly. Preserving the
in-turn correction loop while the controller is offline is separate work.

## Completed verification

1. **Journal and process lifecycle:** both protocol formats survive severed and
   undrained uplinks. Checks cover terminal input fencing, bounded output,
   immutable snapshots, broker credential exclusion and socket cleanup, runtime
   fallback, and process exit. Real Linux SSH/setsid checks confirmed completion,
   labelled output, Patch checksums, and process-group absence after disconnect.
2. **Collection and authority:** API checks recover the original answer and Patch,
   apply one graph revision, refuse duplicate collection, and deduplicate usage.
   Missing, mismatched, unsafe, and incomplete evidence cannot launch a provider.
   Interrupted correction passes retain the operational answer for display.
3. **Repair and episodes:** rejected Work Patches use existing manual repair.
   Collection preserves invocation accounting, target, authority, and Stop;
   replay after an accepted Apply returns the same revision. Auto-research
   incomplete collection does not become an automatic paid retry.
4. **Recovery races:** unreachable or still-alive probes spend no attempt.
   Restart restores pending collection, including interrupted collection children;
   shutdown and superseding continuations stop stale timers. Incomplete verdicts
   and uncollected stages survive operational retention.
5. **Served journey:** Chromium against a disposable served app observed automatic
   graph and chat catch-up without a Collect/Retry request. Reloading and reopening
   the chat still showed one answer and one Apply. Network, console, and server
   logs were clean. The browser test explicitly reports a skip when its Chromium
   prerequisite is absent; the API regressions always run.

Current behavior belongs to the provider and conversation specifications.

The existing supersession boundary remains: collection stands down after a newer
continuation or episode report takes over. The ended-episode regression verifies
collection before report admission and refusal after that takeover, preserving
the newer report, ending, and budget. Collecting across that boundary would need
separate stage ownership coordination and is outside this implementation.
