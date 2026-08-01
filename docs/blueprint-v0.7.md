# RCP — Blueprint v0.7

This document amends [`blueprint-v0.6.md`](blueprint-v0.6.md). Everything in
v0.6 remains authoritative except where this amendment explicitly replaces it.
The design rationale is retained in
[`handoff-2026-08-01-experiment-control-loop.md`](handoff-2026-08-01-experiment-control-loop.md).

## 0. What changed in v0.7

RCP adds a narrow control loop for Experiment nodes and a generic, durable
watcher mechanism. These are separate systems:

- the experiment loop owns readiness, attempt records, bounded retries, and
  graph admission;
- a watcher only checks external work and wakes the conversation that armed it.

External submission remains agent-owned Work. RCP does not become a scheduler,
does not parse submission output, and does not infer an experiment attempt from
a watcher.

## D24 — Experiment readiness includes the attempt budget

An Experiment node's **Run** action is available only when all four conditions
hold:

1. every `governed_by` decision is decided with a selected option;
2. none of those decisions has a pending proposal;
3. no `blocked_by` blocker is open;
4. the number of recorded attempts is below the experiment's configured ceiling.

The gate reads those two base relations by name, not the action layer as a whole.
A custom action-layer relation out of an Experiment therefore gates nothing —
RCP cannot know what "satisfied" means for a relation someone invented — and a
pending proposal gates only when its own operations would change the decision,
not when it merely names one in `related_node_ids`.

`Experiment` gains `attempt_ceiling`, a positive integer with default `5`.
The Experiment editor changes it as an ordinary human draft edit; the canonical
value changes through Sync, and only that canonical value affects readiness.

Readiness is derived. Reaching the ceiling disables **Run** until the human raises
the ceiling. Ordinary Work conversation remains available. V1 shows an advisory
active-loop marker but never locks the repository or prevents human edits. The
marker is derived from a queued or running `experiment_loop` operation or a
nonterminal `ExperimentAttempt`; generic watcher rows never define it. While the
marker is active, RCP refuses a second **Run** for that experiment. This is
duplicate-loop suppression, not a repository lease: the marker remains advisory
about repository use, and ordinary Work stays live.

## D25 — The bounded loop spends attempts, not Work turns

A human presses **Run** to authorize a loop within the current ceiling. RCP opens
a Work turn with the governing decisions and their selected options pinned. No
attempt exists merely because the button was pressed.

Here and below, an attempt means the nested graph model `ExperimentAttempt`.
Provider task attempts created by Pause, Resume, or Retry are operational task
lineage and never count against an experiment ceiling.

Preflight is bounded work inside that turn. The agent may repair ordinary setup
errors and rerun preflight without spending an attempt. A provider failure before
launch is an ordinary failed task with the existing Retry lifecycle and consumes
no experiment attempt.

Once external work is actually launched, that Work turn's patch appends the
attempt record with the pinned decision bundle. A later report closes it. A
proposal-only iteration is the explicit exception: it consumes one attempt
without launching external work, so the loop cannot evade its ceiling by
alternating proposals and retries.

`ExperimentAttempt` gains three backward-compatible fields:

- `attempt_kind`, defaulting to `external_run`, with `proposal_only` as the other
  value;
- `decision_bundle`, defaulting to an empty list for old records, whose strict
  items contain `decision_id`, `decision_revision`, and `selected_option`;
- optional `debug`, a strict object containing `mechanical_fault`, `change`, and
  `predicted_effect` together.

Before a debug retry launches, its `debug` record states:

- the mechanical fault being addressed;
- the change made;
- the predicted mechanical effect.

A disappointing scientific result is not a mechanical fault. The attempt log and
ceiling make this prompt rule inspectable.

Every loop turn receives the concrete count, such as `3 of 5 attempts used`. At
the ceiling, the final watcher wake may inspect logs and write evidence, a
blocker, or a proposal, but its prompt forbids another long-running launch. This
operational limit is a prompt contract because Work retains Bash; RCP adds no
shell parser or report-only permission profile.

A proposal turns the ordinary readiness predicate red. After the human resolves
it, the loop does not resume automatically: the human presses **Run** again.

The existing experiment `completion_criteria` remains optional and advisory. If
present, it is pinned for the loop and shown with the proposed evidence at human
acceptance, but it never controls start, retry, or exit. The loop cannot rewrite
it; an agent that wants it changed must propose that change for human action.

## D26 — Experiment-loop graph authority is narrow

The loop uses `kind="experiment_loop"`, whose admissible graph operations are:

- append or close its own attempt records;
- write its own experiment `status`;
- create evidence and blockers;
- assert epistemic edges;
- attach what it created to its own experiment — `produces` to a new evidence
  node, `blocked_by` to a new blocker — never to a node it did not create;
- create proposals on upstream decisions.

It may not set standing, decide a decision, change hypothesis status, edit its
pinned bundle, or use `experiment.status` as a control input. Validation, not the
prompt, enforces this graph boundary. Belief changes remain human-authoritative:
the loop asserts an evidence edge and Inbox acceptance makes it accepted.

An experiment-loop turn that launches or terminates an attempt must reflect that
fact in its graph patch. A preflight-only provider failure has no attempt to
record. As with ordinary Work, a rejected graph reflection never erases
successful operational work or causes that work to run again.

RCP does not intercept shell launches. If the provider launches external work
and then dies before writing the attempt patch, RCP cannot synthesize the missing
`ExperimentAttempt`. The failed Work task and retained receipts remain visible;
recovery uses the existing task Retry, whose internal actions are not part of
this control protocol. This is an accepted v1 gap.

## D27 — `watch.json` is a generic Work deliverable

After launching external work, a Work turn may write `watch.json` in its reusable
conversation stage. The file is a non-empty JSON list with this complete shape:

```json
[
  {
    "check_command": "a self-contained shell command with literal identifiers",
    "log_path": "/absolute/path/to/output.log",
    "cwd": "/absolute/check/working/directory"
  }
]
```

The agent calls no watcher API or tool. After the provider turn ends, RCP's
existing stage-deliverable collection finds `watch.json`; that file is the arming
request and the validated SQLite records are the acknowledgment.

The file may contain one watcher or N. It carries no experiment, attempt,
project, host, authority, delivery, or handle field. RCP binds the execution
host, originating conversation, and continuation policy from the originating
operation. Those are routing facts, not watcher semantics: ordinary Work wakes
with ordinary Work policy, while a Run-loop watcher retains the narrower
`experiment_loop` patch policy.

`watch.json` is not a graph-change channel. `patch.json` remains the only file
from which RCP reads graph changes. Before every fresh Work turn, RCP removes an
old `watch.json` from the reusable stage and refuses to launch if it cannot.

Every check is observational by prompt contract: it does not submit, cancel,
kill, or modify anything. The launched work must outlive the provider turn, and
the check must work from a cold login shell in its declared `cwd`. Job or process
identifiers are literal in the persisted command; ambient launch-turn variables
are invalid.

## D28 — Watchers poll, persist, and notify independently

RCP validates a `watch.json` list atomically. Its check runner starts a fresh
`bash -lic` in the declared `cwd`; for a remote check, that command goes through
the existing SSH login-shell transport used for provider execution. Every run
has a hard timeout:

| exit | meaning | action |
|---|---|---|
| `0` | watched work is gone | ready an attributed wake |
| `1` | watched work remains in the system | keep polling |
| other | the check cannot answer | initial correction, or degraded retry after arming |

An initial non-0/1 result arms none of the list. RCP resumes the same provider
session with the exact validation error and permits a bounded, watch-only rewrite
of `watch.json`; operational work is never repeated merely to repair the file.
After arming, a non-0/1 result marks only that watcher degraded, records the
error, and keeps retrying. Errors never imply completion.

Each accepted item becomes an independent SQLite watch record with its own
polling state and `notified` flag. Watch records survive RCP and desktop restart.
RCP does not poll while its process is closed; reopening resumes checks and wakes
immediately if a check now returns 0. The watcher store and poller are a separate
lifecycle from provider-task attempts; they may reuse queue primitives but are
not another state on `background.py`'s Pause, Resume, and Retry machine.

Completed, unnotified watchers with the same conversation and compatible
RCP-bound continuation context are coalesced when the wake turn is assembled.
Watchers from one `watch.json` list are compatible; RCP never merges wakes whose
patch policies or pinned Run lineages differ. RCP atomically creates one queued
Work operation and marks only its included watchers notified, so a restart can
neither lose nor duplicate the handoff. The operation reassembles current
conversation context; its bound continuation policy determines graph authority,
so the agent payload cannot choose or widen it.
One distinctly labelled watcher turn lists every completed watcher's RCP id and
`log_path`; still-running watchers remain open. It queues behind a live human
turn and never impersonates a human message. Once queued, ordinary provider-task
Pause, Resume, and Retry own that turn's delivery without re-firing the watcher.

V1 adds no stale-watcher cleanup primitive or retention policy. Watch records
are cheap and remain restart-durable.

## D29 — Control v2 begins with live observation

The main Control v2 goal is a second watcher delivery shape: wake on bounded new
log output while the external work is still running. It uses file-backed logs and
durable output offsets; its trigger schema, batching, debounce, and repeated-wake
semantics remain open.

Stale watcher cleanup and an enforceable repository lease are secondary v2
questions. Graph-level scheduling across the research frontier remains deferred
and is not implied by this amendment.

## v0.7 acceptance

- [`acceptance/S41-bounded-experiment-control.md`](acceptance/S41-bounded-experiment-control.md)
  covers the experiment loop.
- [`acceptance/S42-watchers-wake-conversations.md`](acceptance/S42-watchers-wake-conversations.md)
  covers generic watchers.

Both were confirmed by the human on 2026-08-01 and remain pending until the
implementation passes them.
