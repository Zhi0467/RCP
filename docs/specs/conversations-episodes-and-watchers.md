# Conversations, episodes, and watchers

This specification owns ordinary conversations, bounded Experiment control,
common episode lifecycle, watcher observation/delivery, and visual wrap-up.
Auto-research-specific orchestration and graph branches are in
[Auto-research and branch merge](auto-research-and-branch-merge.md).

## Discuss and Work turns

Discuss and Work are explicit per-turn modes in one conversation. Submit time
captures the mode; Pause, Resume, Retry, and correction preserve it. Changing the
composer affects only the next ordinary turn.

- **Discuss** reasons and answers with no repository mutation or active Patch.
- **Work** authorizes operational execution within its exact project write
  scope and one optional semantic `patch.json`.

A Work turn may finish without a Patch; no net graph change spends no revision.
The answer and graph outcome remain independently visible. A stray Patch left by
Discuss is retained as a receipt and discarded; a file cannot grant its author a
different mode.

## Native chat context

Chat is not transcript ingestion. Canonical chat history exists for display,
but RCP never reads, indexes, copies, projects, validates, or authorizes from
prior RCP transcript text. Provider-native session continuation may retain the
provider's context without making the displayed transcript an RCP input.

The first ordinary turn in an RCP-owned native session receives one master
context containing the current graph target and head, focused node, exact
run-scope repository pointers, enabled-package pointers, schemas, outputs, and
both Discuss and Work contracts. Seeing both contracts grants no cumulative
authority: each turn carries one explicit mode marker.

Later ordinary resumes repeat only the master-context path, then send the marker,
logical turn id, human message unchanged, resolved artifact directory, and a
compact replacement delta only when stable context changed. The repeated path is
a pointer, not an instruction to reread unchanged context. A new baseline commits
only after a mechanically successful turn and is bound to provider, host, native
session, project, graph target, conversation, and focused node. Failed or
interrupted work does not advance it.

An exact conversation/native session cannot be reused across a different chat
or graph target. Main and branch-bound stages fail closed instead of silently
continuing with the other target's authority.

## Conversation scratch and human input

One conversation owns one reusable scratch stage because provider-native resume
depends on its original working directory. Each logical turn owns one exact
`turns/<turn-id>/artifacts` directory. Stale `patch.json` and `watch.json` are
cleared fail-closed before a new turn that could misattribute them. A committed
native chat-session context retains that stage, including its immutable master
context, even while no turn is active.

The desktop composer may turn one bounded macOS dictation segment into editable
text. It never sends automatically or retains audio. Temporary input attachments
are claimed atomically for one nonblank Discuss or Work message, bounded by the
current file allow-list and size/count limits, staged immutably on the execution
host, and reused exactly by task recovery. A partial or unprovable transfer
fails the task rather than dropping files and running text-only.

Attachment bytes, hashes, and paths never become canonical chat or graph data.
Chat history retains only display metadata and expiry. Files are untrusted
temporary context and cannot be the sole durable provenance for Evidence.

An assistant answer also supports temporary selection comments for the next
human turn. Selecting answer text opens a comment composer beside the selection;
each submitted comment becomes one editable or removable composer annotation,
and the main composer shows their count. Several annotations may be staged. On
send, each contributes only its copied selected text followed by
`comment: <comment>` to the ordinary human message. There are no message
references, source identifiers, offsets, durable annotation records, or graph
authority. Staging clears when the turn is accepted and otherwise remains a
client-side draft for that chat.

## Common episode parent

Auto-research and Experiment-loop are modes of one persisted episode parent.
The parent owns identity, human authorizer, graph target, lifecycle, durable
ending, native-session binding, operational ceiling, Stop state, report state,
and restart reconciliation. Mode adapters own their distinct admission,
authority, watcher/child settlement, and compact wrap-up facts.

The parent's recorded human authorizer is the authority for every turn inside the
episode, so a different current human pressing Resume or Retry cannot stand in for
it. An episode with no recorded authorizer therefore has no recoverable turn, and
RCP refuses that recovery by naming the situation and the remaining action — a
fresh human Run, which starts a new episode and records its own authorizer.

Every episode has exactly one validated native-session binding at a time:
provider, session id, execution host, exact reusable stage, project, graph
target, and actor conversation. A human Run always starts a fresh episode and
fresh native session. A provider switch is a deliberate recovery that becomes
active only after a mechanically successful handoff; automatic work never
silently switches or starts fresh.

Only operational provider turns spend the operational ceiling. Validation,
same-invocation Patch/watcher correction, exact Resume/Retry, and hidden report
generation do not consume another operational unit.

## Experiment readiness and budget

An Experiment can start a new bounded episode only when:

1. each `governed_by` Decision is decided with a selected option;
2. none of those Decisions has a pending Proposal;
3. no `blocked_by` Blocker is open; and
4. no current episode still has a queued/running automatic invocation or a
   deliverable live watcher; and
5. no episode parent is still live at all. A turn can succeed below the ceiling
   while arming no observer and taking no exit, which leaves the parent live with
   nothing to wake it. The loop then reads as inactive, admission still refuses a
   second live parent, and **Stop loop** is the control that releases it; and
6. the Experiment itself is not `completed`, `abandoned`, or `superseded`.

Readiness reports its graph gates and its operational reasons as separate lists,
so no surface has to tell them apart by reading the sentences.

Graph prerequisites derive from the exact graph target's final graph. A closed
Experiment separately refuses a fresh episode: Runs says the Experiment is
complete and offers no episode-start action until a human or already-authorized
graph-writing task edits the node back to a nonterminal status. This fresh-start
gate does not revoke an invocation already authorized inside the current episode.
Before any episode the action says **Start episode**; after history exists it says
**Start new episode**. The node's current `invocation_ceiling` becomes the new
episode's pinned operational ceiling. Historical episodes retain their pinned
used/ceiling values while the current node value remains separately visible as
**Next episode limit**.

Starting an episode does not create an ExperimentAttempt. Attempts are semantic
agent-authored bookkeeping and never control budget, watcher identity, or
episode admission. A nonblank human initial goal is retained exactly; only blank
input receives the RCP fallback objective.

Only the newest unresolved operational task in the newest episode may perform
operational Resume or Retry. Patch-only repair may reflect retained completed
work but cannot rerun side effects or reopen an old episode.

## Experiment-loop graph authority

Each invocation receives a dedicated Experiment contract and compact control
file with phase, episode, graph target, invocation counts, pinned Decisions,
current drift, completion criteria, and delivered watcher identities. Watcher
state is a separate exact file. The provider never receives prior chat
transcripts. An automatic wake repeats one path to the full Experiment contract
that initialized its exact current native session; it does not tell the provider
to reread that unchanged contract.

The Experiment-loop Patch may update its own attempt/status and guidance, create
Evidence and Blockers, assert legal epistemic and output edges, and create the
permitted Proposal shapes within its pinned upstream/tested boundary. It may not
set standing, decide a Decision, apply a Hypothesis transition, change its pinned
Decision bundle, remove nodes, or treat Experiment status as automatic
invocation control. Fresh human episode admission owns the closed-status gate.

Validation and Apply both use the episode's exact project and graph target.
When a child belongs to an Auto-research episode, every graph context, Patch,
watcher, correction, settlement, and report input remains branch-targeted.

## Graceful Stop and recovery

**Stop loop** persists intent before returning and before an unclaimed compatible
watcher can win a new claim. It means: finish the already-authorized turn, retain
its valid Patch and semantic result, stop existing and newly emitted compatible
watchers, and admit no automatic continuation.

If no unresolved task remains, Stop settles immediately. While the current turn
is queued, running, or pausing, Runs shows **Stopping gracefully** and recommends
waiting. If that turn pauses, fails, or is interrupted, the episode shows
**Needs action** and only the exact available Resume, Retry, or Switch-provider
recovery. Recovery cannot clear Stop or reenable watcher delivery.

Stop does not cancel external work, delete watcher history, edit Experiment
status, create or close an attempt, or discard a valid Patch. If the exact saved
session is unusable, Stop may durably abandon only recovery of that already
terminal task while preserving history, then settle.

Budget exhaustion starts no automatic wake. Pending completion remains visible
and unconsumed. Once the final operational turn settles, non-Stop endings enter
wrap-up; a later human **Start new episode** creates the only counter reset and
may claim retained compatible completion as invocation one.

## Watcher resources

Conversation and Experiment watcher targets are separate exact resources, not a
client-chosen mode field.

- A conversation `watch.json` wakes that same conversation.
- An Experiment watcher resource is keyed by project, exact graph target,
  Experiment node, and compatible episode, and wakes that bounded episode.

A branch watcher can never wake a main task, and a main watcher can never spend
a branch episode. Watcher selection, staging, atomic claim, task creation, and
episode association retain the same target.

Every watcher file has two all-or-none lists:

- `external` observations with a literal `check_command`, absolute `log_path`,
  and absolute `cwd`; and
- `graph` conditions from a closed vocabulary.

The graph vocabulary is exactly: a named node reaching one of named statuses,
or a named Proposal being resolved after arming. There is no arbitrary query,
standing predicate, new-node arrival, or relation predicate.

## Graph-condition delivery

Graph conditions evaluate at accepted revision boundaries and at startup, using
the exact target's canonical transition order. A staged draft never fires them.
Halted/degraded replay means not yet. A node removed after arming retires its
condition.

Each condition stores its arming head. A node status already true at that head
is immediately ready; Proposal resolution is prospective and requires the
specific later resolution event. A resolved Blocker transition is observed from
the retained final node/event, with no delete operation.

Every graph wake spends one permitted invocation, including one caused by a
human Sync. External and graph completions that become ready together coalesce
into one target-consistent wake. Canonical event watermarks make crash/restart
delivery idempotent.

## External observation

External checks run in a cold login shell with a hard timeout. Exit `0` means
the named work is gone, `1` means still present, and any other result is
unobservable. Active observations use the normal interval; repeated failures
persist bounded exponential backoff and identity jitter. Only exit `1` resets
the error count. A degraded observation is never inferred complete or dead.

Experiment watchers may form immutable groups of at least two new observations.
A group wakes once when no member remains active and every nonretired member is
either complete or persistently unobservable at the capped tier. The latter is
diagnostic readiness, not scientific success. Stop/disposition items may retire
only a staged compatible external observer after the agent has settled its work;
they cannot retire graph conditions or claim RCP cancelled the process.

Initial validation, grouping, retirement, replacement, and insert commit
atomically. One invalid item arms none. An empty final watcher declaration is
legal only with a success, Proposal, or Blocker Patch exit. Missing or malformed
handoff enters same-session correction without spending another unit and may not
repeat operational work.

## Watcher maintenance authority

An authorized node Work chat may maintain only its focused Experiment resource;
authorized project Work may maintain live Experiment resources in that project
and exact graph target. Discuss may inspect staged state but cannot mutate it.
Origin chat, provider, path, or maintenance machine grants no authority.

Maintenance uses its own Work task/session, spends no Experiment invocation,
does not create an attempt, and never replaces the episode's native-session
binding. Stop, watcher claim, and competing maintenance have one atomic winner.

## Visual wrap-up

Completion, operational exhaustion, unrecoverable failure, and a human-authority
pause fence new operational work and enter one hidden report wrap-up. Explicit
Stop is the only ending that declines it. An ending whose turn never bound a
provider session has no session to resume and so terminalizes directly, with no
wrap-up record and no report error; that is an absence of a report, not a failed
one, and it never leaves the episode on a live wrap-up status.

Report generation resumes the exact episode session and stage with only the
durable ending, the official report-skill/output pointer, and one compact
immutable mode receipt. It never rebuilds or resends the graph or transcript.
The hidden allocation permits at most three provider turns total, clears the
exact output before each attempt, and spends no operational unit.

If shutdown interrupts or pauses an in-flight hidden allocation, startup
requeues that same operation rather than creating another allocation. The
transaction clears its prior write-scope fingerprint and records a reserved
dispatch-reset fence newer than the old worker's attempt receipts. Only that
durable fence makes the requeued operation launchable; public receipt writers
cannot forge it, and the previous attempt remains inspectable history.

A valid `episode-report.html` is captured as bounded immutable HTML and served
in the opaque artifact sandbox. The report has no Patch, watcher, command,
Proposal, or graph channel and never determines the episode verdict. Final
report failure is a durable visible nonblocking error with no manual report
Retry; the episode still terminalizes. It is shown beside the ending it belongs
to, never as the episode's health, its recommended next step, or a reason to
withhold a control.

## Runs projection

The backend Experiment-control projection derives one health, one Runs section,
one **Recommended next step**, and the exact available controls from structured
episode, task, Stop, budget, watcher, report, and owning-node state. Raw task and
report state remain supporting data and do not compete as peer episode states:
the ending fence alone decides that an episode is over and which episode controls
it retires. Once the episode is terminal, a closed owning Experiment makes the
Runs object Completed even when that episode ended by human Stop; the stopped
ending remains inspectable history. The browser renders the published answers
and never reconstructs them from a fresher task list. Controls appear only when
currently valid, and no recommendation and no diagnostic names an unavailable
action. Report availability is independent of current-episode selection: the
backend publishes the newest available report for the same Experiment and exact
graph target, so a later stopped episode cannot hide an earlier durable report.
The stopped episode remains current history; the report link retains its actual
owning episode id.

The runtime, parent episode, visible task rows, usage meter, and latest available
report used for one Experiment-control answer come from one SQLite read snapshot.
Resume, Retry, and provider switch target only the exact current operation named
in that answer; a missing task row yields no client control.

The experiment detail retains exact target, episode history, pinned budgets,
current next-episode limit, current guidance validity, watcher provenance and
groups, session continuity, diagnostics, and report. Ordinary conversations and
Paper coaching remain outside Runs.

## Verification contracts

The durable observable journeys are [S10 recovery](../acceptance/S10-pause-resume-retry.md),
[S40 Discuss and Work](../acceptance/S40-discuss-and-work.md),
[S41 bounded Experiment control](../acceptance/S41-bounded-experiment-control.md),
[S42 conversation watchers](../acceptance/S42-watchers-wake-conversations.md),
[S53 truthful Runs projection](../acceptance/S53-truthful-attention-and-run-surfaces.md),
[S76 graph-condition wake](../acceptance/S76-graph-condition-wake.md),
[S78 one budget and Stop](../acceptance/S78-one-budget-one-stop.md), and
[S120 visual episode report](../acceptance/S120-episodes-wrap-up-with-a-visual-report.md).
