# Handoff — the control half: experiment gates and the bounded loop

**Date:** 2026-08-01
**State:** design discussion, settled between the human and the agent. **No code
has been written.** The resulting amendment is [`blueprint-v0.7.md`](blueprint-v0.7.md),
and its two proposed acceptance contracts are
[`S41`](acceptance/S41-bounded-experiment-control.md) and
[`S42`](acceptance/S42-watchers-wake-conversations.md). The human confirmed both
on 2026-08-01.

Read `AGENTS.md` first, then [`blueprint-v0.7.md`](blueprint-v0.7.md), then this
file. This file records a design conversation: what was settled, what
was explicitly rejected and why, and what is still open. It is not yet a promise
the app makes.

---

## 1. What this is about

Everything RCP does today runs one direction: **conversation → graph**. Sources
index provider JSONL, agents emit patches, materialization replays them, humans
hold authority at the Inbox. The graph is a derived record of what was said.

Control reverses the arrow: **graph → action → observation → graph**. The graph
stops being only a record and starts determining what gets dispatched.

The scope settled here is deliberately narrow: **experiment nodes only.** That
is where compute and time are actually spent, so it is the highest-ROI place to
make the graph load-bearing. A graph-level scheduler across the readiness
frontier was discussed and explicitly deferred.

---

## 2. The two clocks

An external job produces observations continuously. Most mean nothing. They
cannot enter `.research/patches/` — a poller would generate thousands of
revisions of "still running" and replay would become the bottleneck.

- **Operational store** — mutable, restart-durable, non-canonical, and truthfully
  stale. Watch definitions and poll results, plus the existing provider-task
  lifecycle. Poll results are re-derivable; watcher rows remain in SQLite across
  restart, including after notification because v1 adds no cleanup policy.
- **Epistemic log** — the existing append-only patch log. Low frequency,
  revision-bearing, human-authoritative.

**The seam rule: an observation crosses into the graph only when it changes a
claim or terminates an attempt.** "Job running, as of 12s ago" is operational.
"Attempt 3 stopped, log shows OOM at step 210" is a patch.

---

## 3. The executable subgraph already exists

The layers in [`core/models.py`](../src/rcp/core/models.py) already separate
what is true from what must happen before what. This was not designed for
control, but it works:

- **Epistemic edges are conclusions.** `supports`, `weakens`, `refutes`,
  `contradicts` are learned *after* work happens. Nothing waits on them.
- **Action edges are preconditions.** `governed_by`, `blocked_by`,
  `requires_decision`. An experiment governed by an open decision genuinely
  cannot start.
- **Seam edges are the I/O contract.** `tests` says what the node is for,
  `produces` says what it emits.

So the input gate needs no new relation types. It is a predicate over existing
edges and upstream state plus the experiment's attempt count and ceiling.

**Readiness is derived, never stored.** The moment a separate list of "what to
run" exists, it drifts from the graph and becomes a stale todo list. This is the
failure mode that kills research-management tools, and it is not optional.

---

## 4. The input gate

An experiment is runnable when all four hold:

1. every `governed_by` decision has status `decided` with a `selected_option`,
2. none of those decisions has a **pending proposal** against it,
3. no `blocked_by` blocker is open,
4. its recorded attempt count is below its configured ceiling.

Green gate → the Run button is live. Red → greyed out.

At the ceiling, the human may raise the ceiling deliberately. Ordinary Work chat
remains available; the disabled Run button prevents another automatic loop start,
not human investigation.

The active-loop marker is also derived: a queued or running `experiment_loop`
operation or a nonterminal `ExperimentAttempt` makes it visible. A generic
watcher never does. While visible, RCP refuses a second Run for that experiment,
but ordinary Work stays available. This suppresses a duplicate control loop; it
is not a repository lock or lease. The marker remains advisory about repository
use.

**Agent changes to decisions are always proposals.** This replaces an earlier
"agents may add preconditions but never remove them" rule, which was strictly
worse: it could not express *over-constraint* (the agent discovering a decision
is too tight), and it required a special rule where the existing gated-proposal
mechanism already suffices.

Consequences worth knowing:

- A pending proposal on a decision gates **start** for every experiment governed
  by it, and **flags** any that are already running. RCP never terminates a
  running experiment.
- **There is no pause primitive.** The agent pauses its own loop by writing a
  proposal on an upstream decision, which turns the gate red. Pause is the
  readiness predicate returning false for a reason the agent authored.
- A proposal **consumes an attempt** from the loop's ceiling. Otherwise a loop
  escapes its budget by alternating propose/retry forever.

Pinning still matters for exactly one path: a human editing a decision directly
through an `approval` patch, which never passes through a proposal. An attempt
pins its governing decisions' revision and selected option at submit; if a human
moves one mid-run, the next wake carries the diff.

---

## 5. The bounded loop

**Trigger.** A human presses Run. Every debug iteration inside the pinned budget
is then automatic. *Humans authorize spend envelopes; loops spend within them.*
After a proposal is resolved the loop does **not** auto-resume — the human
presses Run again. Every restart of spend is a human act.

**Attempts.** Once a run is launched, that Work turn's patch appends its attempt
record; a later agent report closes it. The explicit exception is the
proposal-only iteration in §4, which consumes one record without launching work.
The count is simply how many records exist — nothing needs classifying to
increment it. Ceiling is a per-node `attempt_ceiling` field, a positive integer
tunable in the Experiment editor and defaulted to 5. Raising it is an ordinary
human draft edit: only after Sync makes the new value canonical does Run become
available again.

The attempt schema stays a graph record and adds only what this loop needs:
`attempt_kind` (`external_run` by default or `proposal_only`), a structured
`decision_bundle` of decision id/revision/selected option, and one optional
all-or-nothing `debug` object containing the mechanical fault, change, and
predicted effect. Defaults keep existing attempts replayable.

This means the graph's nested `ExperimentAttempt`. Provider task attempts created
by Pause, Resume, or Retry are separate operational lineage and never count
against the experiment ceiling.

Preflight happens before an attempt. The agent fixes ordinary setup errors and
reruns bounded checks inside the same Work turn. A provider failure before any
launch or proposal is an ordinary failed task with Retry and consumes no attempt.
If preflight discovers a missing human decision or external blocker, the agent
uses the existing proposal or blocker exit; there is no generic pause primitive.

Every loop turn sees the concrete budget, for example `3 of 5 attempts used`.
At `5 of 5`, the watcher wake may inspect logs and write the final evidence,
blocker, or proposal, but the prompt tells it not to submit another long-running
run. This is a prompt contract because Work still has Bash; RCP does not parse
shell commands or add a special report-only permission profile.

**The retry rule is prediction-landing, not failure classification.** Enumerating
failure types was considered and rejected: any taxonomy will be wrong, and the
log belongs to the agent as context, not to RCP as a control input. Instead each
debug attempt states, *before* running:

- what it thinks is mechanically wrong,
- what it changed,
- what it expects that change to fix.

The next attempt reports whether the predicted effect materialized. Repeated
predictions that do not land are visible evidence that the agent's model of the
bug is wrong; the experiment ceiling prevents that pattern from becoming an
unbounded retry loop. This is the precommit-and-check principle recursing one
level down: the debug loop is a small experiment loop, judged against a
prediction written before the outcome.

**A debug attempt must name a mechanical fault, not a dissatisfaction with the
result.** "Loss went to NaN at step 40" is a fault. "Accuracy lower than
expected" is a finding. This is the guard against the most dangerous confusion
available here — a loop that reads a disappointing result as a misconfiguration
and iterates until it passes, which is p-hacking implemented in software. It is
a prompt contract with a visible record, not an enforcement; the attempt log and
the retry ceiling are what make it inspectable.

**Exits (closed set):**

| Outcome | Loop writes | Human sees |
|---|---|---|
| Ran to completion | evidence node + asserted outward edge | one Inbox item: accept the edge |
| Mechanically blocked or ceiling exhausted | blocker node | blocker on the node |
| Needs a decision | proposal on the upstream decision | Inbox gated card |

A scientifically completed experiment always produces exactly one human
authority item. **It cannot complete silently.** A blocker is instead visible on
the node, and a proposal uses its existing Inbox path.

**Belief update stays human.** The loop asserts the `supports`/`weakens` edge;
only a human accept makes it `accepted`, and only accepted evidence counts
downstream. This needs no new mechanism — it is the existing standing model.

**Success criteria are optional.** An earlier draft made a precommitted success
criterion a mechanical exit condition. It was dropped: in this loop the exit
does not depend on it, and a thing that is not a gate should not be mandatory.
What it actually buys is *cheap accepts* — with a threshold written beforehand,
accepting an edge is checking one comparison instead of re-deriving the whole
interpretation from artifacts. So: the existing optional `completion_criteria`
field on the experiment, shown next to the proposed edge at accept time, has no
mechanical role. When present it is pinned for the loop, not rewritten per
attempt. An agent that wants it changed must propose that change for human
action; otherwise it could weaken the criterion after seeing attempt 1's numbers
and make precommitment theater.

---

## 6. Enforcement — the anchors

The loop is an optimizer. Its graph-authority rules must be structurally out of
reach. Those rules are an **admission contract, not a prompt contract** — the same
distinction the repo already draws between Codex's sandbox and Claude's
`acceptEdits` boundary. The operational attempt ceiling is the stated exception:
because Work retains Bash, not launching another run at the ceiling is the prompt
contract in §5.

[`core/validation/ops.py`](../src/rcp/core/validation/ops.py) already branches on
`ctx.patch.kind` for gated updates, standing, belief causes, and proposal
resolution. The anchor is that mechanism one notch finer:
`kind="experiment_loop"`, whose admissible operations are

- append to its own attempt records,
- write its own experiment `status`,
- create evidence and blockers,
- assert epistemic edges,
- attach what it created to its own experiment: `produces` to a new evidence node
  and `blocked_by` to a new blocker, both refused for any node the same patch did
  not create,
- create proposals on upstream decisions,

and which may **not** `set_standing`, decide a decision, change hypothesis
status, or edit the pinned bundle. Validation rejects the patch.

**`status` is cosmetic, and the rule is a read restriction, not a write one:
nothing may read `experiment.status` for control.** The moment a gate consults
it, an agent-written field silently becomes scheduling policy. The input gate
reads edges, decision and proposal state, blocker state, and the recorded attempt
count—never `experiment.status`.

**Human off-gate Work is not blocked.** The Run gate governs the bounded loop,
not the existing Work authorization. Ordinary Work remains available while Run
is disabled, and RCP does not parse its shell commands into control events.

---

## 7. The monitor

**What it is for, in one sentence: start an agent turn when watched external work
leaves the system.** It does not judge outcomes, classify failures, or know what
any job system is.

### 7.1 The deliverable

The agent launches the external work itself, through its own Bash, and then writes
one file into the conversation's run stage — the same scratch folder it already
writes `patch.json` into:

```json
[
  {
    "check_command": "ids=$(squeue -h -o '%A') || exit 2; grep -Fxq 4471 <<<\"$ids\"; case $? in 0) exit 1;; 1) exit 0;; *) exit 2;; esac",
    "log_path": "/w/swa/runs/a1-seed0.log",
    "cwd": "/w/swa"
  },
  {
    "check_command": "ids=$(squeue -h -o '%A') || exit 2; grep -Fxq 4472 <<<\"$ids\"; case $? in 0) exit 1;; 1) exit 0;; *) exit 2;; esac",
    "log_path": "/w/swa/runs/a1-seed1.log",
    "cwd": "/w/swa"
  }
]
```

The file is a non-empty list: one turn may arm one watcher or N. Every item has
exactly those three fields. Then the turn ends. That is the entire agent-facing
surface. There is no watcher daemon, polling code, persistence, or RCP plumbing
for the agent to write. The agent calls no watcher tool: after the provider turn
ends, RCP's existing stage collector discovers `watch.json`, and successful
validation plus the resulting SQLite rows acknowledge arming.

That folder is reused by later turns in the same conversation. Before every
fresh Work turn, RCP removes any old `watch.json` and refuses to launch the turn
if it cannot. After arming, the poller reads the saved watch records in SQLite,
not this file. Therefore a file left by yesterday's turn cannot be mistaken for
one written by today's turn.

**Every identifier is literal.** `4471` and `4472` are the real Slurm job ids the
launch returned. Not `$JOB_ID`, not `$PID`, not a placeholder — RCP runs each
string in a fresh shell hours later with none of the agent's context. A temporary variable
created inside the command itself, such as `ids` above, is fine; an ambient
variable from the launch turn is not. An earlier draft of this file had `test !
-d /proc/$PID` as an example, which is exactly the bug.

**No handle protocol.** A design round proposed the launch printing an opaque
handle that RCP captures and re-injects as `RCP_RUN_HANDLE`. For Slurm the handle
is the job id; for a bare process it would need to include the PID and its start
identity. The abstraction bought nothing over letting the agent substitute the
literal identity it already has. Rejected.

### 7.2 What the check command means

It answers exactly one question: **is this thing still in the system.** Queued
counts as still there. It is *not* "is it executing."

This rules out a whole class of plausible-looking wrong answers. `pgrep -f
train.py` is broken: a job sitting in the queue has no process, so the first poll
reports stopped and the loop gets a false wake before the run ever starts.
The Slurm queue is the right authority because a queued job is still in
`squeue`.

The exit contract applies to each watcher independently:

| exit | meaning | RCP does |
|---|---|---|
| `0` | gone | wake an attributed agent turn |
| `1` | still there (queued or running) | keep polling |
| other | the check could not answer (ssh down, command not found) | record the error, keep polling, no wake |

The first execution during arming is stricter: an `other` exit rejects the whole
list for correction, because RCP has not yet proved the commands work from cold.
After arming, an `other` exit marks only that watcher degraded and records the
error; RCP keeps retrying it on later polls. A later 0 or 1 clears the degraded
state. An error is never treated as completion.

The Slurm check above is deliberately more careful than `test -z "$(squeue
...)"`. That shorter form falsely reports completion if `squeue` itself fails.
Expanded, the contract is:

```bash
ids=$(squeue -h -o '%A') || exit 2
grep -Fxq 4471 <<<"$ids"
case $? in
  0) exit 1 ;;  # still queued or running
  1) exit 0 ;;  # absent from the queue
  *) exit 2 ;;  # the check itself failed
esac
```

A bare `/proc/9321` check is not durable because Linux can reuse PID 9321 for an
unrelated process. A local-process check must compare both the literal PID and a
process-start identity captured at launch. That check is OS-specific, so this
handoff does not pretend there is one portable shell template for it.

### 7.3 The two things the agent must get right

Both are prompt contracts. RCP does not parse the launch command or try to prove
either property.

**The launch must outlive the turn.** The process chain is `RCP → ssh → agent
process → agent's bash tool → the job`. The turn lasts minutes; the job lasts
hours. When the turn ends the SSH session closes and anything left in its process
group is SIGHUP'd. Foreground in the agent's bash tool → the turn blocks for
hours and never ends. Backgrounded with a bare `&` → the job dies with the
connection. Fix: `setsid` / `nohup` / `sbatch` / tmux, so the job's parent is
init or slurmd. `sbatch` gives this for free; a bare workstation does not, and an
agent will get it wrong by default. A foreground launch simply leaves a visibly
stuck Work turn until it is stopped or retried. That failure is accepted; there
is no RCP turn timeout pretending to solve it.

**The check must run from cold.** No reliance on ambient state that existed only
during the turn — a `module load` from twenty minutes ago, an activated conda
env, a directory the agent happened to be standing in. If the check needs a
module loaded, the check loads it.

The agent verifies both before ending its turn, and verifies the check *the way
RCP will run it*: fresh login shell, declared `cwd`, nothing inherited. Running
it inside its own accumulated shell proves nothing.

### 7.4 Arming

RCP reads the non-empty list in `watch.json` and adds the routing facts it already
knows: the execution host, the conversation to notify, and the originating
operation's continuation policy. The agent supplies none of them. These facts do
not make a watcher part of an experiment or attempt. Ordinary Work therefore
wakes with ordinary Work policy, while a Run-loop watcher retains the narrower
`experiment_loop` patch policy. RCP validates the whole list and executes every
check once before persisting anything. If one item is invalid or returns an exit
code other than 0 or 1, RCP arms none of them and returns the whole file for
correction.

**That execution is a proof that the command works from cold, not an assertion
about the job.** Stopped is a fine answer (the job was short; wake now). Still
there is a fine answer (arm the watch). `command not found` is the failure, and
it goes to a watch-only correction round: RCP resumes the same provider session,
returns the validation error, and asks it to rewrite only `watch.json`, at most
twice. This is a new use of the correction pattern already used for `patch.json`,
not behavior the current implementation provides automatically.

An earlier draft required this check to report still-running. That was wrong — a
short job can legitimately finish in the gap, and a Slurm job can legitimately be
queued.

Once the list passes, RCP creates one internal watch record per item. They poll
independently. When a wake turn is assembled, RCP includes every completed,
unnotified watcher with the same conversation and compatible RCP-bound
continuation context in one attributed wake. Every watcher from one list is
compatible; watchers with different patch policy or pinned Run lineage are not
merged. Records still running remain open. Every check execution carries a hard
timeout.

### 7.5 Where everything runs

- **The run stage and check** live on the execution machine. If the agent ran
  remotely, that is the remote host, and RCP reads the file the same way it reads
  `patch.json` today. A scheduler may run the actual job on a compute node; the
  execution host only needs to be able to query it and reach its log.
- **The poller lives in the RCP process**, on a background worker off the web
  event loop, over watch records in SQLite: watcher id, conversation id, host,
  origin operation and continuation policy, check command, log path, cwd,
  interval, last polled at, last exit, notified. This is a watcher lifecycle
  separate from `background.py`'s provider-task attempts; it may reuse the
  durable queue, but it is not another Pause/Resume/Retry state.
- **The check runner starts a fresh `bash -lic` in the declared `cwd`.** Locally
  RCP invokes that wrapper directly. Remotely it goes through the existing SSH
  login-shell transport used for provider execution. Do not hand-roll `ssh host
  "<check>"`: a plain non-interactive ssh sources no profile, so `module`, conda,
  and PATH additions are missing and `squeue` may not even resolve. The loop
  prompt gives the agent this exact runner shape for its cold verification.

The check is observational: it does not submit, cancel, kill, or modify anything.
That is a prompt contract, just like correct detachment. RCP enforces the hard
timeout but does not attempt to prove that arbitrary shell is read-only.

### 7.6 Durability

The requirement is **surviving a restart**, not advancing while RCP is closed.

On startup RCP reloads the open watches and resumes asking. A watch whose RCP was
shut for three days is simply asked again; if the answer is now `0`, it wakes
immediately. **There is no catch-up or reconciliation, because there was never a
transition to miss** — completion is detected by asking, not by observing. For
Slurm, the literal job id is either still present in the current queue or absent
when RCP asks again. Bare PID-only checks do not satisfy this durability rule.
This is the property that makes polling strictly better here than
OpenClaude's parent-child detection, which requires a live parent at the instant
of exit.

Each watch record has its own `notified` flag. When the conversation is free,
RCP atomically creates one queued Work operation for the completed watchers and
marks only those records notified. A restart can therefore neither lose the
handoff nor create another operation for the same watchers. From that point the
existing provider-task Pause, Resume, and Retry lifecycle owns delivery.

The node shows truthful staleness — "last checked 3 days ago" while RCP was shut,
updating on the first poll after open.

**Accepted consequence: the loop's wall clock is bounded by when the human opens
the app.** Close it Friday, the job finishes Saturday, the debug turn happens
Monday. The job itself was never at risk. This is one attempt overnight instead
of three.

Making the loop advance unattended is not a monitor change — a daemon that
notices completion cannot start an agent turn, assemble context, or take the
append lock, so it would buy only a timestamp. It is the separate and much larger
question of whether RCP runs headless with the window as an optional client,
which is `S28`/`S30`/`S31` territory and should be decided on its own terms.

### 7.7 Wake provenance

Two open bugs in OpenClaude are our design decisions here:

- *Completion notifications interrupt an active turn.* → **Queue the wake behind
  a human turn in flight.** A human turn may change the node, and a racing wake
  would reason against stale context. Their `now`/`next`/`later` priorities are
  the right vocabulary; we need `next`.
- *Notifications arrive in the user slot, so the model responds as if the human
  spoke.* → For RCP this is the authority boundary, not cosmetics. A wake is a
  **distinctly attributed turn with an immutable visible label**, exactly like
  the per-turn Discuss/Work labels, and the assembled context states in one line
  that this is an automated watcher result and not a human instruction. The node
  conversation then reads as one honest transcript: human turns, loop turns, and
  watcher wakes, each unmistakably from whom.

The wake is a fresh Work operation because only Work can arm `watch.json`. It
reassembles current context instead of resuming the old provider turn. RCP also
retains the originating operation's patch policy: ordinary Work stays ordinary,
and a Run-loop wake remains `experiment_loop`. The watcher payload cannot choose
or widen either capability.

The wake lists each completed watcher's RCP-assigned id and `log_path`, so the
agent knows which watchers fired. Compatible completions for that conversation
that accumulate before the turn starts are coalesced into this one wake, not N
Work turns. None of these values grants new authority.

### 7.8 Taken from OpenClaude

Read at `Gitlawb/openclaude`: `MonitorTool`, `src/Task.ts`,
`src/utils/task/framework.ts`, `src/utils/messageQueueManager.ts`.

Taken into v1:

- **A `notified` ledger** coupled atomically to the durable queued Work operation.
- **Permissions are the RCP process's local or remote login user.** The check is
  read-only by prompt contract; no new permission profile or parser is added.

Deferred, not rejected, as the **main goal of Control v2**:

- **Output to a file plus an `outputOffset` cursor.** Never hold a log in memory;
  read bounded deltas from disk. Resumable by construction.
- **A second delivery shape: wake-on-new-output.** V1 wakes only when a check says
  the watched job has left the system. V2 adds bounded live observation so a
  doomed run can be diagnosed and stopped at hour two instead of hour nine.

Not taken: their completion detection is parent-child and their task record lives
in in-memory `AppState`. It does not survive app close and does not need to.

### 7.9 Rejected alternatives

Recorded so they do not get proposed back.

**MCP tool over a reverse SSH tunnel.** Proposed as `arm_watch`, with a
per-launch injected MCP server and an operation-scoped token, tunnelled back to
the local RCP process. Rejected: the repo already has the required pattern —
invariant 4b makes file-in-scratch the one way a structured deliverable leaves an
agent, and the bounded resume-and-rewrite path used for `patch.json` is extended
to validation errors for `watch.json`. That is the acknowledgment the MCP design
was built to provide. The tunnel adds a network service on a shared login node,
a hard dependency on
`AllowTcpForwarding` (commonly disabled on hardened clusters), per-provider
injection config, token minting, port allocation and teardown. And nothing in the
turn needs a round trip: the agent is stating a fact, not asking a question. What
*was* right in that proposal is kept — RCP supplies the routing context, performs
the arm-time check, times checks out, and attributes queued wakes.

**Wrap-and-mark.** RCP wraps the launch so the job writes an exit-code marker
file, and RCP polls for the file. One flaw poisons everything downstream: a hard
kill (OOM killer, `scancel -9`, node failure) writes no marker, so *no marker*
means either running or dead and the filesystem cannot say which. Recovering from
that required heartbeats, signal traps, an `sacct` tiebreak, and a special case
for submit-and-detach — Slurm-specific machinery patching one self-inflicted
flaw. The check command asks the authority instead of inferring from a file's
absence, so the flaw does not exist and there is no "unknown" state.

**RCP-owned submission.** Proposed to close the foreground-launch hole: the agent
writes `{launch, check, log}` and RCP performs the launch, enforcing that it
returns in seconds and capturing the id atomically. Rejected on ownership — the
agent owns operational execution, RCP owns the gate, the watcher, the wake, and
graph admission. The two benefits do not justify moving process execution across
that boundary when detachment is already an accepted prompt contract.

**The admitted gap:** RCP does not intercept Bash. If the agent launches work and
then crashes before writing the files, RCP cannot synthesize the missing graph
attempt or arm a watcher. The failed Work task and retained scratch and receipts
remain visible. Recovery is the existing task Retry; this design does not
prescribe what the agent does inside that Retry.

---

## 8. Worked example — a training job

Graph before: `H4` "sliding-window attention at 4k window stays within 1 point
of full attention at 32k context"; `E9` "train 1.3B, sliding-window vs. full",
`tests → H4`; `D2` "compute budget for the ablation", options `4×A100` /
`8×A100`, selected `4×A100`, `decided`, `E9 governed_by D2`. Ceiling 5.

1. **Gate green.** D2 decided, no pending proposals, no open blocker. Run live.
2. **Human presses Run.** RCP opens a Work turn on E9 on gpu01 and pins D2 @ rev
   118 / `4×A100`. No attempt exists yet.
3. **Agent** prepares `job.sh`, runs bounded preflight (`--dry-run`), then
   submits: `sbatch --parsable job.sh` → `4471`. Its loop patch records that
   launch as attempt 1 in the experiment log. Separately, the agent verifies its
   check from a fresh login shell in `/w/swa` — the queue query finds literal id
   `4471` and exits 1, still there — then writes `watch.json`:
   `[{"check_command": "ids=$(squeue -h -o '%A') || exit 2; grep -Fxq 4471 <<<\"$ids\"; case $? in 0) exit 1;; 1) exit 0;; *) exit 2;; esac", "log_path":
   "/w/swa/runs/a1.log", "cwd": "/w/swa"}]`. **Turn ends; nothing is held.**
4. **RCP arms.** Reads the list, records the Work conversation and execution host,
   and executes every check through the fresh login-shell check runner. This
   one-item list passes, so RCP persists one watch record and polls it on the
   configured watcher cadence. The experiment's attempt log remains a separate
   part of the Work loop.
5. **14 min later** the check exits 0. RCP queues an attributed wake and opens a
   fresh Work turn labelled `[watcher]`.
6. **Attempt 2.** Fault: `CUDA OOM at step 210, activation memory`. Change:
   gradient checkpointing. Prediction: peak memory under 68GB, passes step 210.
   Resubmits, verifies the new check against job `4488`, writes a new one-item
   `watch.json` list.
7. **6h later** the check exits 0. Log shows `NCCL timeout, rank 2` at step
   4100 — checkpointing pushed step time past the 12h wall at 4 GPUs. The fix is
   not code, it is D2.
8. **Attempt 3 is a proposal, not a run.** Agent proposes reopening D2. RCP:
   D2 now has a pending proposal -> E9's gate goes red -> Run greys out. The loop
   is paused by the same predicate that started it.
9. **Human** approves in the Inbox. D2 -> `8xA100`, rev 121. Gate green. The loop
   does not auto-resume; the human presses Run.
10. **Attempt 4** pins D2 @ rev 121, submits, arms. 7h later the check exits 0,
    log is clean, metrics written.
11. **Loop exit.** One patch: `E9.status = completed`; attempt 4 closed; new
    evidence `V17` (*"val ppl 12.41 full vs 13.88 SWA-4k; 32k; 1.3B; seeds
    0-2"*) with an artifact ref; `E9 produces -> V17`; `V17 weakens -> H4`,
    asserted. Nothing else is admissible.
12. **Human** accepts the edge. `H4` -> `weakened`, belief transition recorded
    with this evidence as cause.

**The watcher judged only that Slurm job 4471 left the queue. The agent read the
artifacts and proposed the scientific interpretation; the human accepted it.**

---

## 9. Open questions

1. **Repository lease (v2).** V1 does not hard-lock a repository or prevent human
   edits; it shows only an advisory active-loop marker. Whether later control
   needs an enforceable lease remains open for v2. Human authority must remain
   explicit either way.
2. **Graph-level scheduling.** Deferred by decision. Nothing currently asks
   whether *this* experiment is still the best use of the next four hours; that
   stays a human judgement for now.
3. **Control v2 — live output.** This is the main goal after the completion-only
   v1 loop: add wake-on-new-output using file-backed logs and durable offsets.
   Decide the watcher schema, what counts as a useful output event, how repeated
   wakes are batched or debounced, and how the agent stops a doomed run without
   confusing an observation wake with completion.
4. **Control v2 — stale watcher cleanup.** RCP watchers do survive app close and
   restart. Their records are cheap, so v1 adds no cleanup primitive or retention
   policy beyond normal completion and notification. Cleanup for permanently
   abandoned or degraded watchers remains an explicit v2 lifecycle question.

---

## 10. Next action

**The two acceptance scenarios were confirmed on 2026-08-01.** Experiment
control and generic watchers are deliberately separate promises:

- [`S41`](acceptance/S41-bounded-experiment-control.md) covers the gated Run
  path, attempt log, bounded retries, and human-authoritative exit.
- [`S42`](acceptance/S42-watchers-wake-conversations.md) covers the reusable
  OpenClaude-style watcher mechanism, including N watchers, restart durability,
  error handling, coalesced delivery, and its lack of experiment semantics.

Plan and implement against
[`blueprint-v0.7.md`](blueprint-v0.7.md). Do not collapse these scenarios into
one lifecycle during implementation.

Nothing in this file has been implemented, and nothing in it has been verified
against a running system.
