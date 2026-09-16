# A finished turn survives a lost link

Date: 2026-09-15
Status: drafted 2026-09-15 from the incident below, which was read out of the
live database, the provider session logs, and the run stages on the execution
host. The diagnosis is evidence-backed, not inferred. The design shape was
settled with the human in the same session: the pipe stays the control channel,
the journal is a durability record, live behavior is unchanged, and no remote
validator is required. Nothing is implemented yet.

Close this handoff when a turn whose link dies during execution is collected on
reconnect with its answer and its patch, when the two recovery defects in slice 1
are fixed, and when the collect path is exercised by a test on every run rather
than only by a real disconnect.

## The incident, from the record

Two Work turns were sent from a laptop that then slept. All times UTC.

1. `22:09` and `22:19`. Two conversation Work turns start on the same remote
   execution host, one on Codex, one on Claude. Both are ordinary chats, not
   episodes (`episode_id` is null on both).
2. `22:19`–`22:58`. Both agents do their work. Both edit their project
   repositories, run tests, write documentation, and write `patch.json` to
   their run stage. Every one of those file changes survives and is on disk
   today.
3. `22:38`–`22:55`. Each agent calls back to RCP to validate its patch. Four
   requests total, two per turn. No response file is ever written, because RCP
   is on the sleeping laptop.
4. Both agents handle that correctly. The Claude turn's final report opens:
   "All verification is complete and the Patch passes the local schema check;
   the live RCP validator was unreachable twice." The Codex turn's says: "live
   validation has not returned. It is not applied." Neither blocked on it.
   Neither needed it.
5. `22:44:47` and `22:58:10`. Both turns produce a complete final answer, in
   the provider's own session log. RCP receives neither.
6. `22:51`. RCP marks both turns failed. Both `provider_exit` receipts carry
   the same cause: `ssh: Could not resolve hostname …`, and
   `remote_process_stopped: false`. RCP could not reach the host to stop
   anything, so it fenced both chats.
7. `23:31`. The automatic transport reattempt fires for both. It was armed with
   a 30-second timer at `22:51`; the timer was suspended with the machine and
   fired the moment the lid opened, before the network was back. Both
   reattempts died in under a second against an unreachable host, and the
   failure was not classified `transport_lost`, so the chain ended at attempt 1
   of 3.
8. The Claude process exited on its own. The Codex process did not: it had
   finished its turn at `22:44:47` and was still emitting events into a stdout
   pipe whose reader had stopped draining. It blocked mid-write and stayed
   alive, holding the stage fence, for three and a half hours until a human
   session terminated it through `remote_terminate_provider`.

A third turn on the same host at `00:44` hit the identical transport loss and
recovered by itself, because by then the machine was awake and its 30-second
reattempt landed on a reachable host. That contrast is the cleanest statement of
the defect: nothing about the mechanism is unsound, and everything depends on
whether a laptop happens to be open.

## What the incident proves

The remote side already survives. It is detached with `setsid`, reparented to
`init`, keeps its own pidfile and durable start/stop receipts, and its stage is
preserved. The agents finished, answered, and wrote their patches.

What does not survive is RCP's participation. A turn is delivered as a live
stream over one SSH connection, and nothing durable is written on the remote. So
a lost link does not interrupt the work; it destroys delivery of work that has
already been paid for and completed.

## Defects

1. The broker's uplink to RCP is one-shot stdio over one SSH connection. A new
   connection cannot take it over.
2. Provider events are never journaled on the remote, so there is no offset a
   reconnect could resume from.
3. There is no continuation that adopts a finished, undelivered turn. `retry`
   discards the native session (`session_id: None`) and re-runs cold;
   `repair-graph-update` refuses, because `repairable` is only set when RCP
   received a patch and rejected it. A patch RCP never saw is unreachable by
   either.
4. The automatic transport reattempt is a wall-clock timer that fires on wake
   before the network returns, and the resulting stage-probe failure is not
   classified `transport_lost`, so the chain ends at attempt 1 of 3.
   `RemoteRunStage.directory_exists` already separates "the stage is gone" from
   "the host could not be asked"; `attach` collapses both.
5. A Codex process whose stdout stops being drained blocks instead of exiting,
   holds the stage fence, and can only be cleared by an out-of-band terminate.

## Design

### The pipe stays exactly as it is

When RCP is reachable, nothing changes: the live stream, live validation, and
the in-turn correction loop all behave as they do today. The pipe is not a
delivery mechanism that a file could replace. It is the control channel, and it
carries things a journal cannot:

- the stop fence. Reading a `result` event is what makes RCP close stdin and
  terminate before yielding, because a provider can otherwise read stdin as a
  second turn. Interposing a file opens that window.
- backpressure. A provider whose reader stops is supposed to block. Remove that
  and a runaway turn writes unbounded output to the staging filesystem instead.
- steering acknowledgements, the broker's command request/response, the broker
  ready line, the startup-hold release, and stderr.

### The journal is a durability record

Every turn writes, under its stage at `turns/<operation_id>/`:

- `events.jsonl` — provider events, appended as they are forwarded
- `answer.md` — the final labelled answer
- `patch.json` — as today
- `outcome.json` — the terminal receipt: return code, protocol completion,
  token usage

Writing is unconditional, because a turn cannot know in advance that its link
will drop. Reading is not: RCP reads the journal only when collecting.

### Collection

A new continuation adopts a finished, undelivered turn. It launches no provider.
It attaches to the stage, reads `outcome.json`, `answer.md`, and `patch.json`,
commits one transition, and settles the turn.

Collection feeds the paths that already exist. A collected patch that fails
validation becomes an ordinary rejected graph update with `repairable: true`,
which is the repair path that exists today and that this incident could not
reach.

The unresolved-pass fence stops being a dead end and becomes the trigger to
collect.

### What collection cannot do

It recovers a finished turn. It cannot resume an interactive one. Steering
writes to `process.stdin` and waits for its acknowledgement in the stream, and
after a reconnect there is no stdin to write to. A turn that was still mid-flight
when the link died is collected up to its last journaled event and settled as
incomplete; it is not resumed.

### No remote validator

Considered and rejected for this work. The agent-facing contract already has
`unavailable` as a first-class validation status, and both agents in the
incident used it correctly and finished. A remote-side validator would preserve
the in-turn correction loop while offline, which is a real benefit and a
separate project. It is not required to stop losing work.

## Slices

1. **Recovery defects.** Gate the transport reattempt on a reachable host rather
   than elapsed wall-clock, and carry the `directory_exists` distinction through
   `attach` so an unreachable host is classified `transport_lost` and keeps the
   chain alive. Ships on its own and is independently useful.
2. **Journal.** Write `events.jsonl`, `answer.md`, and `outcome.json` on every
   turn. No read path yet. Bound the journal so a runaway turn cannot exhaust
   the staging filesystem.
3. **Collection.** The continuation that adopts a finished, undelivered turn,
   its route, and its projection.
4. **Codex stop.** A finished Codex process must not be able to block on an
   undrained stdout and hold the fence.

## Verification

The collect path must be exercised by a test on every run, not only by a real
disconnect. The automatic reattempt in this incident existed, was wrong, and
went unnoticed until its receipts were read.

- A turn whose link is severed mid-execution is collected on reconnect with its
  answer and its patch, and applies one revision.
- A collected patch that fails validation lands as a rejected graph update whose
  existing repair path works.
- A severed turn that had not finished is settled as incomplete, and is not
  resumed and not re-run.
- Slice 1 alone: a reattempt armed before a sleep does not consume an attempt
  against an unreachable host.
- The episode path takes the same launcher and the same broker, so it needs the
  same severed-link check; episodes are the unattended case this matters most
  for.
