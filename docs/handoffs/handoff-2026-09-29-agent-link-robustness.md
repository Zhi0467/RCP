# Agent link robustness

Date: 2026-09-29
Status: design only. Investigation continues in this PR. No code yet. The human
starts implementation on this same PR after the investigation below closes and
an xhigh design review has run.

Implemented: nothing.
Remaining: fixes A–C, investigation D, then its fixes.
Settled with the human on 2026-09-29:

- RCP does not manage provider auto-compaction. The provider owns it.
- In the UI, a chat's watcher wake appears in that same chat. That stays.
- A chat watcher wake should continue the chat's native session.
- Retry, branch merge, seed, and refresh keep starting fresh sessions.
- No fix is shaped by the machines this evidence came from. No preferred binary
  path, no `PATH` reordering, no host, OS version, or provider special case.
  A fix states the contract it needs from any machine, and RCP checks that
  contract or removes the dependency.

## Evidence

Read from real provider transcripts and task events, 2026-09-18 to 2026-09-28,
on a personal space with an SSH GPU host and on a team server.

- A chat's watcher wake started a new native session each time, with the full
  36.5k-character Work contract. Three wakes in 20 minutes each re-explored and
  ended at 90–150k context. The chat's own session never saw what they found.
- One wake failed on a parse error in RCP, not in the agent (fix B).
- On the SSH host, the live Patch validator hung in 3 of 5 calls. Each time the
  validator's poller had already stopped, minutes before the call (fix C).
- On the SSH host, Apply was rejected in 2 of 4 turns with
  `rsync(PID): error: unexpected end of file`. Five such rejections since
  2026-09-11. One left the graph stale for 7 days, until a later turn redid it
  (investigation D).
- Agents wrote `patch.json` and `watch.json` correctly in every turn. Every
  watcher registered, including on turns whose Apply was rejected, and fired.

## A. Chat watcher wakes continue the chat's session

Today:

- `_generic_watcher_delivery_request` in `src/rcp/api/app.py` builds the wake
  with `session_id=None`.
- `start_watcher_notification` in `src/rcp/runs/watcher_admission.py` refuses
  a watcher wake that carries a session, unless it is an Experiment wake.
- A human chat turn continues only because the Web client picks the session:
  `latestNativeSessionId` in `web/src/agentTasks.ts` takes the newest task in
  the chat that has any native session. The server has no such rule.
- Every prompt owner classifies from the session id it was given
  (`classify(LaunchPhase(session_id=…))`). So the full contract is the correct
  prompt for the launch as built. The launch is what is wrong.
- Side effect: after a fresh wake, the chat's newest session is the wake's. The
  human's next turn continues that session, not the one they were talking to.

Target:

- The server resolves the chat's current native session and stage. The Web
  client uses the same server rule, so there is one owner.
- The wake launches on that session with the `wake` continuation node: the
  delta and the master pointer, not the full contract.
- Admission accepts a session on a chat wake, bound to that exact chat.

Open questions for review:

1. Which task counts as the chat's current session? The client takes the newest
   task with a session, whatever its status. A failed or interrupted turn may
   leave a session that cannot resume.
2. What does **New session** in the chat header leave behind for the server to
   see, so that a wake after it starts fresh?
3. A wake while a human turn runs in the same chat. Today the wake is a
   separate session, so both can run. On one session they must not.
4. The watcher records the provider and model of the turn that armed it. If
   the chat's current session is from another provider, the wake cannot use it.
5. No resumable session: start fresh and say so in the task history, or refuse?
   Invariant 10g forbids a silent fresh fallback for episodes; chats have no
   rule yet.

Checks: a test that drives a chat wake end to end and asserts the recorded
prompt is a delta with a pointer on the chat's session. Then one real-provider
wake on disposable data.

## B. One event parser splits frames on newlines only

`_event_from_sse` in `src/rcp/background.py` splits a frame with
`str.splitlines()`. That also splits on `\x85`, ` `, and ` `, which
JSON leaves unescaped inside strings. An agent that prints such text, for
example binary output, cuts the frame's JSON in half. The background task then
fails with `Invalid JSON: EOF while parsing a string`. Every background task
reads its events through this function.

Fix: split on `"\n"` only. Other readers use `removeprefix("data: ")` and are
not affected.

Check: an event whose text holds each of those characters round-trips through
`_sse` and `_event_from_sse`.

## C. The validator's poller survives a blip and a detached turn

A live validation call travels: agent client, then the staged broker beside the
provider on the execution host, then a request file in the stage, then RCP's
poller, which lists the stage over SSH.

Two things stop the poller for the rest of the turn:

1. `serve_command_mailbox` in `src/rcp/agents/command_mailbox.py` exits on the
   first failed listing. On exit it expires the turn's credential, which can
   serve exactly one turn, so a restarted poller cannot take over.
2. When the stream drops after the host accepted the turn
   (`remote_result_pending`), the Work turn closes its validator mailbox. The
   remote agent keeps working, for 27 minutes in one case, with no poller.

In both hangs the turn's stream and the poller failed within 20 seconds of
each other: one lost link.

#225 made each call return within 90 seconds. It does not bring the poller
back.

Target:

- A failed listing is retried with bounded backoff inside the same loop. The
  credential stays active. Each outage and recovery is a task event.
- The mailbox keeps being served while the turn is detached, until the remote
  provider exits and recorded settlement begins.
- The bounds live in `limits.py`.

Open questions for review:

1. Does the staged broker on the execution host survive the link drop?
   Probably, since the agent kept running, but not yet confirmed.
2. How long an outage before the poller gives up for good, and what the agent
   sees then.

Checks: a test that injects one failed listing and then answers a request, and
a test that answers a request after `remote_result_pending`.

## D. Apply transfers and tool dependencies (investigation in progress)

Findings so far:

- Apply's transfers shell out to a bare `rsync`: the state-tree pull in
  `_sync_remote_tree` and the push in `_publish` (`src/rcp/transport/state.py`).
- The released Mac app starts with `/usr/bin` first on `PATH`, so it runs
  Apple's `openrsync` (protocol 29). A source run from a shell likely gets a
  Homebrew rsync 3.x. CI runs GNU rsync on Linux. No test runs the rsync the
  app runs.
- Nothing on the desktop checks `rsync` or `ssh` locally, or `rsync` and
  `python3` on the execution host. Server install and doctor check Linux
  server tools only.
- `unexpected end of file` is openrsync's message when the remote rsync ends
  mid-transfer. Reproduced by killing the remote side. In that run the remote's
  own message was in stderr too. RCP kept only the openrsync line.
- Ruled out: a file vanishing mid-transfer (exit 24, clear message) and a
  killed local ssh (exit 20, no message).
- A rejected Apply is not retried. The graph stays stale until a later turn
  notices and redoes the patch.

Hypotheses still open:

1. The link dropped. It fits one of the five cases, where the stream dropped
   first.
2. The shared SSH master closes 60 s after its last use
   (`SSH_CONTROL_PERSIST_SECONDS`). A transfer that joins it as it closes is cut
   mid-stream. Apply's rsync uses the shared master, not a partition.
3. Something on the host ended the remote rsync.

Plan:

1. Tool audit: every external tool RCP runs, on the Mac and on the execution
   host. For each: how it is resolved, what version it assumes, what checks it,
   and how its failures are classified.
2. Reproduce hypothesis 2 with RCP's exact SSH options, or rule it out.
3. Keep the full stderr and the exit code of every failed transfer.
4. Decide how Apply moves files. Leading option: drop rsync from Apply and the
   state sync, and move these small files with RCP's own shipped scripts over
   plain SSH, one mechanism on both ends. Alternatives: bundle a pinned rsync,
   or state a minimum rsync contract and check it on every machine that runs a
   transfer. Picking whichever rsync one Mac happens to have is not an option.
5. Decide whether Apply retries a transient transfer failure itself.

## Wider scope

The goal is that RCP's core works across machines, platforms, and temporary
states. Investigation D covers these as well:

- RCP-managed tools: the staged command client and broker, and the remote
  scripts.
- Connections: SSH masters, partitions, reattach after a drop.
- Dependencies: `rsync`, `ssh`, `git`, and `python3` on the execution host.
- Temporary states: sleep and wake, a lost link, an update mid-turn, a
  provider usage limit mid-turn.

Each finding lands here first, as a fix or as a decision.
