# Conversations, episodes, and watchers

This specification owns ordinary conversations, bounded Experiment control,
common episode lifecycle, watcher observation/delivery, and visual wrap-up.
Auto-research orchestration and episode graph branches are in
[Auto-research and branch merge](auto-research-and-branch-merge.md).

## Agent names

One agent is one Agents-board conversation (`chat_id`). Its name is the stored
`chat_display.title` for that project and conversation, shared by the board,
transcript responses, and Bulletin. Creation uses the first Markdown heading
in the first message when present, otherwise the whole message, with whitespace
collapsed, cut to `AGENT_NAME_MAX_CHARS` (35), and trailing spaces trimmed. An
empty node-chat message uses the node id, then "Chat". An Auto-research chain
starts with its branch name, cut the same way. Graph consolidation starts as
"Graph consolidation", explicitly seeded by its admission owner, and cannot
self-name. Later turns and branch renames do not regenerate names.
Existing conversations receive the same initial name during migration; human
renames are preserved, and every stored name longer than the cap, human renames
included, is cut the same way. Transcript-only legacy conversations are seeded through
the same storage owner when first read; cached summaries refresh their stored
name independently of task target matching.

A member rename overwrites that field and records the member in
`titled_user_id`. Empty or over-cap names are refused, and the rename input
stops at the cap; there is no reset to a derived name.
An agent may choose one short name for its own conversation, only before either
an agent or human has named it. Storage enforces this once-only choice and the
same cap. Archiving or unarchiving keeps the name.

**Why:** a researcher should find the same agent by the same name in every view,
and separate Experiment episodes must not create separate agent identities.

## Human notification observations

The sender reconciles main-target attention after startup and accepted main
transitions, never on API readiness. It takes each project's local episode
baseline before serving and observes episodes before remote graph reads. Slow
or unreachable graphs do not block other projects' episode observations.
Graph items awaiting reconciliation stay held; failures retry next pass.

Observation runs even with notifications disabled, so enabling a kind does not
replay old attention. Markers and per-device outbox rows advance atomically.
The first baseline sends nothing and represents the graph before serving;
accepted-transition signals prevent it swallowing new attention. A late signal
recovers only swallowed attention without moving markers backwards or replaying
delivered receipts. Delivered rows expire after their 24-hour window. Update
pauses finish the in-flight project read and leave unreached projects dirty.

Every 15 seconds the sender observes `(health, blocked_reason)` for unfinished
or not-yet-terminally-observed episodes, using the API's batched health logic.
`needs_action` and `wrapping_up` with `sign_in` use **Needs you**; `completed`,
`stopped` and `failed` use **Episodes finished**. Existing episodes baseline
silently; new episodes notify on their first eligible observation, even if they
ended between passes or during downtime.

Each question creation produces one durable **Needs you** event, linked to its
chat or episode. Delivery is independent of episode health; a health change
caused by the question adds no second push. Dismissal, wakes and reauthorization
do not recreate the event.

## Discuss and Work turns

Discuss and Work are explicit per-turn modes in one conversation. Submit time
captures the mode; Pause, Resume, Retry, and correction preserve it. Changing the
composer's mode or configuration affects only the next ordinary turn; while the
running attempt can take input, the composer's message uses that attempt's
runtime behavior instead, as described under conversation scratch and human
input below.

- **Discuss** reasons and answers with no repository mutation or active Patch.
- **Work** authorizes operational execution within its exact project write
  scope and one optional semantic `patch.json`.

Work pursues the requested outcome through the investigation, execution,
verification, and repair it needs. The agent inspects results and iterates while
useful authorized work remains, rather than stopping at the first attempt or
returning feasible next steps for the human to perform. It finishes when the
outcome is achieved, hands off ongoing work through the watcher contract, or
explains the concrete unavailable prerequisite or new authority needed for
further useful progress. This does not widen the requested objective or grant
another invocation.

Every turn settled by ordinary Work (human Work chat and nightly consolidation) writes
`outcome.json` in its exact task stage: `status` is `done`, `partial`, or
`could_not`; `reason` is optional for `done`, and required and non-blank otherwise.
The reason is one line of at most 240 characters. The answer is never parsed for
an outcome. Experiment loops, Auto-research, child Work, Discuss, and Seed/Refresh
are outside this contract. New turns clear stale outcomes with the other handoffs.

A missing or invalid outcome gets one same-session correction through the watcher
handoff correction loop, without another invocation, budget, or repeated operational
work. If still invalid or absent, it records `not_stated` without failing the turn.
Recorded-result recovery cannot launch a correction and records `not_stated` when
no valid outcome is available. A Retry treats an `outcome.json` left unchanged from
its predecessor as missing. Only the Work owner sets the outcome; provider event
text never does. Task results and assistant transcript messages carry
`turn_outcome`; user messages and non-Work turns have no outcome. Project transfer
carries it in the transcript only; transfer task records omit it so the record
schema version stays unchanged. The outcome is independent of the graph receipt. Honest partial or couldn't-do-it results are good
results; done must be backed by what the turn ran or read. Unknowns are stated once,
plainly, rather than hedging every sentence.

A Work turn may finish without a Patch; no net graph change spends no revision.
The answer and graph outcome remain independently visible. A stray Patch left by
Discuss is retained as a receipt and discarded; a file cannot grant its author a
different mode.

Browser consent is an app-local per-chat preference, off by default. The browser
preference API reads and sets it without rewriting transcripts. Admission of every
chat turn, including a watcher wake and a question answer, snapshots it into
`browser_requested`; changing the toggle affects the next turn.
Imported history does not import consent or browser profiles. Chat transcript messages
project the durable browser status of their turn, including unavailable and lost.
Archiving a chat closes its browser and retains its profile after active work ends.
Turning the toggle off deletes the chat's browser profile, with its logins and
cookies, right after the response; an active turn keeps its browser and the profile
is deleted when that turn ends. Turning Browser back on before cleanup cancels
that pending close and deletion; archive and project-deletion cleanup remain in
force. Pending project-deletion cleanup retains its origin even if the same
project is registered again. A session hears its browser state when it starts
with a browser and whenever the state changes, compared with its last committed turn.
Project removal requests deletion for every retained owner. Failed cleanup stays
pending and retries at later turn boundaries. The owner key names the stable stage,
space, project, and resolved execution host and account; a repointed SSH alias
cannot clean up the old host through its new target.

## Native chat context

RCP chat history is display data, never an input for transcript ingestion or
task authority. Native provider continuation may retain its own context. An
agent the member runs in their authenticated page acts as that member under
[Authority and Proposals](authority-and-proposals.md); a Work message it sends
is new member input, not replayed transcript authority.

**Why:** Page agents use the member's existing session and fixed tool catalog.
A separate actor would duplicate identity without changing where authority comes
from. Protected judgment stays outside the catalog. Voice confirmation defaults
on for Work and episode authorization; terminal commands always require an exact
command/repository confirmation. WebMCP has no RCP confirmation guard. Disabling
voice confirmation accepts misheard speech or injected content starting paid
work. History attributes these actions to the member, not a separate agent.
The [API spec](api-web-and-desktop-projections.md) owns the tool surface.

Task insertion resolves the latest native-session binding of the exact chat in
the same transaction as overlap and stage admission. Client hints and transcript
text cannot select it; resolution never skips an unusable current binding.

- Active and paused-resumable turns block human turns and defer wakes. Wakes
  additionally defer queued, pausing or interrupted current turns; a human may
  continue after interruption.
- Human model changes keep the session. Provider or machine changes start fresh
  with a recorded reason. The composer offers provider/model/reasoning, defaulting
  to the last turn then Settings, but not machine selection.
- Wakes require their recorded provider/model/machine to match. Mismatch claims
  completion once and records a failed notification task without provider launch
  or Retry. Deferred wakes replace one visible reason until admitted.

Every resolution records `chat_session_resolution`. Fresh-session reasons are
`no_session_yet`, `session_history_only`, `session_recovery_abandoned`,
`session_stage_unavailable`, `session_stale`, `session_context_unavailable`, or
`orchestrator_session_unavailable`.
`session_limit` keeps the session. Viewer edits use an artifact's saved session
without making it current; composer comments use the current chat session.
**New session** creates a new chat id. Provider, host, stage, target and scope
remain bound; neither another chat nor another graph target can reuse them.

### Branch this chat

A member can create a new conversation on a new graph branch forked at the
source conversation's current target and head, or on an existing non-archived
branch (including main when leaving a branch). The source conversation and its
stage stay unchanged. Creation records the source chat, its latest operation,
source graph target, title and launch profile; it launches no provider. An active,
paused, imported or unusable latest session cannot seed a branch, and the client
checks this before it creates a new graph branch for the chat. The request carries
a client id that names the new chat, so a retried request returns the same chat.
The new, initially empty conversation is immediately readable and listed on its
own target, which ordinary admission keeps fixed. A node conversation keeps its
node when that node exists on the target; otherwise the new chat has project
scope while retaining the native conversation context. A chat without a native
session of its own, such as a branched chat before its first turn, cannot branch.

Its first human message forks the source chat's latest session as it stands when
that message is admitted, with that turn's provider, model, reasoning and machine;
the same usability rules apply again then. Admission records the source session,
its cwd, its host and the source chat's latest turn on the turn. From then on the
new chat never takes newer source state: Retry and later messages before it has a
session carry that record, and an attempt on another host fails. Fork failure fails
the turn and leaves Retry available; it never falls back to a fresh session. Once
the source chat admits a newer turn or loses that session, a carried fork is
refused before launch (`chat_fork_source_moved` or `chat_fork_source_unavailable`)
as a failed turn without Retry, because the provider can only fork the source's
live state. The launch rechecks this just before the provider starts, so a queued
or slow-starting fork fails the same way, without Retry, instead of taking newer
source turns. When the provider first names the forked session, the same check runs
again in the transaction that would record it; a refusal fails the turn the same way
and never records that session, so a source turn admitted during startup is never
inherited. The failed turn shows the code's shared human reason, as the composer
does for a refused admission. Offline restore detaches a fork that has no session yet, so the new
chat's next message starts a fresh session like any restored chat; its origin
link remains. Once the provider reports the new native
session, later attempts and turns, including a Retry after Pause, resume that
session normally. The fork turn replaces the inherited master, which states the
graph target as every chat master does, and on that turn alone adds a fork note
naming the admitted target. The note tells the agent that the inherited
transcript's graph, Bulletin channel, roster, mentions and paths belong to the
source. Every other branch-scoped fact (branch read pointers, Bulletin snapshot
and roster, agent name, questions, workspace and command client) is rendered each
turn from the new chat's own task.
**Why:** the provider transcript is the only place a stale branch can come from.

The entry point is "Branch this chat" in the chat header's menu. It opens the
branch picker, listing branches to move to and New branch, without merge, rename,
or archive. The top bar's picker names the new chat's target; the transcript's
first line links back to the source chat, named by the source's current title, or
by the title recorded at creation once the source is gone.
Origin metadata remains available after the new chat has its own turns. Browser consent remains the new chat's own preference.
The fork turn always states that preference, even when off, because the forked
session may still hold the source's grant; later turns state it only when it changes.

A generic `watcher_wake` is a new logical turn, clears handoffs and uses the wake
prompt node. Experiment and child wakes keep their own policies. Shared master
restoration, deltas and replacement are owned by
[continuation prompts](providers-and-containment.md#continuation-prompts).
The chat master supplies graph target/head, focus, run-scope repositories, selected
packages and non-secret compute metadata. Separate Discuss/Work contracts grant
only the mode named on each turn. Resumes include the unchanged human message,
logical turn id, artifact directory and changed values. Baselines commit only
after mechanical success and bind provider, host, session, project, target,
conversation and focus. A legacy master without a durable record is reusable
only when its bytes match its filename digest.

Compute selections persist on task and chat records. Added/updated entries send
bounded access metadata; removals send names. They change no execution or graph
scope. Branch composers work during and after an episode with independent human
authorization, sessions and budgets. Older chat records resolve their target from
durable task/session bindings. Branch chats and their recovery preserve that
target. A human may start a separate Experiment episode on the branch without an
Auto-research child route.

## Conversation worktrees

Before a conversation has a Work turn, its composer may select **Work in a
worktree**. The first ticked Work turn binds exactly one run-scope repository on
the execution machine. RCP persists an immutable binding before creation:
project/chat/focus, repository alias, machine and execution host, canonical shared
and worktree paths, Git common metadata directory, real worktree and starting
branch names, and starting commit.
The new branch starts at the shared checkout's captured commit; uncommitted
shared edits are excluded. Detached starting HEAD is refused. A deterministic
sibling path and branch derive from the chat id. Episodes use the same worktree
binding path with an episode owner. A path alone never establishes a binding.

Every later Discuss or Work turn uses the bound repository pointer. Discuss
still has no repository write authority. Native continuation, Pause, Resume,
Retry, and server restart retain this binding; missing or moved worktrees fail
before launch without falling back to the shared checkout. A partially created
binding can prove and finish its exact Git registration; an unregistered orphan
branch requires explicit human repair. Worktree selection cannot change after
any Work turn.

**Integrate** starts an ordinary Work turn with an RCP-authored instruction and
the execution account's own Git/GitHub credentials. The backend offers **Open a
pull request**, **Merge into <starting branch>**, and **Merge into <default
branch>**, hiding the duplicate branch. Default-branch identity comes from
`origin/HEAD`; an unavailable default is explicit, never guessed as `main`.
All integrations refuse dirty worktrees and show the tracked/untracked changes.
Local merges also preflight a clean shared checkout and an existing local target
branch. Pull requests keep worktree-only repository scope and do not require a
clean destination. A local filesystem origin is pushed without invoking GitHub.
The provider rechecks the preflight facts immediately before acting. If the
target is checked out in the shared checkout, merge there; otherwise the provider
may switch to the target inside the worktree and must restore the worktree
branch afterwards, including after aborting its own failed merge. A saved
integration Resume/Retry may find only that operation's exact admitted target
checked out in the worktree; it reruns the same clean-checkout and target-existence
preflight before admission and provider launch. Ordinary turns still require the
bound branch. Chat Integrate leaves Git writes to the provider. RCP never commits
shared-checkout changes, resets, stashes, or force-pushes. Episode Merge has the
bounded automatic path described below. Task completion is not an integration receipt.

**Remove worktree** is explicit, refuses an active/paused turn or dirty worktree,
and serializes with fresh, Resume, Retry, and graph-repair task admission for that
chat. It shows commits ahead of the current starting branch and an explicit `origin`
branch lookup (unknown with a reason if unavailable),
and removes only the checkout. The branch and unmerged commits remain. Binding
removal has a durable intent and terminal tombstone; later turns in that chat
fail clearly and require a new chat. Failed turns retain the worktree. Project
record removal likewise never deletes repository worktrees.

## Conversation scratch and human input

A conversation retains one scratch stage because native resume needs its cwd.
So a saved stage outside the current data directory, after the directory moved
or was copied, cannot resume: that turn fails and records
`continuation_context_unavailable`, and Retry or the next message starts a
fresh session with that recorded reason.
Each logical turn owns `turns/<turn-id>/artifacts`. New turns clear stale
`patch.json` and `watch.json` fail-closed. Committed native context retains its
stage and master even while idle.

Human Discuss/Work chats and human Experiment invocations may `ask`; the question
card receives answers while the composer remains steering. Viewer edits,
consolidation, ingestion, Paper, workers and child Work/Experiments cannot ask.
[The command protocol](providers-and-containment.md#one-graph-output-channel)
owns idempotency, allowed verbs and acknowledgement. Answers never change authority.

Each bounded ask call retains a random `call_id` across transport rounds.
Pending replies carry `attempt` and `max_attempts` (3); parked replies count no
attempt. Clients without an id omit the count. A thread-safe process-local
record retains the last 32 calls per question; restart resets this advice.
Repeat the exact call while blocked, or check later while doing independent
work. After three unanswered attempts, end the turn and leave the card open.
Once maintenance closes the process's work admission, a waiting ask replies
`parked` instead: maintenance locks the human out, so the turn ends and the
answer after reopening resumes the conversation.
Prefer one blocking shell wait; unfinished-command polling uses roughly
five-minute waits.

An answer is received only after client acknowledgement and successful settlement
on its original session and authority binding. Before creating a follow-up, RCP
may steer it into a compatible running chat turn when no ask call is waiting or
has just returned that answer. Only an acknowledged steer becomes its transcript
projection. Acknowledgement precedes projection, so reconciliation repairs a
failed write without delivering again. Refused/unknown steering leaves ordinary
follow-up eligible and shows no false refusal for the answer.

Settlement and startup admit at most one unreceived-answer follow-up, atomically
claimed with task insertion. It keeps the asking turn's mode, native session,
provider, host, stage and target. Both live launch and recorded
settlement verify the complete origin. Provider switches and clean handoffs
cannot receive it; occupied/paused bindings defer, unusable bindings stay visible.
Discuss has no Patch/write scope; Work runs with the scope its launch builds.

Answers project once through StateWorkspace using question id and revision.
Chat follow-up ids derive from that pair; Experiment answers name only a claimed
follow-up. Chat projection first reserves the asking prompt at its creation time
so answers cannot precede it; Experiment transcripts contain no turn prompts.
Stable ids and a projected-revision marker repair partial publication without
republishing. Successful projection does not retire deferred admission. Routes
and settlement reconcile their project; startup sweeps all. Question/receipt
queries do not depend on the receipt display cap or displayed history.

The ordinary composer addresses the watched attempt while it can receive input.
The backend supplies capability, button label and disabled reason from its actual
runtime. No hint line or separate steering control is shown. While steering,
mode is locked to the running turn; the next-turn selection returns afterwards.
Unsupported runtimes leave that next-turn toggle available. Episode workers use
orchestrator mail instead. Steers persist as human messages with delivered,
refused or unknown receipts. Reload never infers acknowledgement from reply text
or resends input. [Provider steering](providers-and-containment.md#live-human-steering)
owns transport races and placement.

[Dictation](api-web-and-desktop-projections.md#dictation) produces editable text,
never sends automatically. Send finishes dictation, then sends once with the
captured target/settings; a changed target or setting keeps the draft instead.
RCP stores no audio. SpeechAnalyzer is local; the older Apple recognizer or a
chosen network service may send audio externally. Audio attachments are refused.

One nonblank Discuss/Work message atomically claims bounded temporary attachments.
They stage immutably on the execution host and recovery reuses the exact batch;
partial transfer fails rather than dropping files. The claimed batch expires
`RUN_STAGE_RETENTION_DAYS` after send; from then on its task offers no Retry or
Resume and both endpoints refuse, since recovery without the exact batch would
change the turn. Chat retains display metadata and expiry, never
bytes/hashes/paths. Attachments are untrusted context, not sole durable Evidence
provenance.

Project references may name stored artifacts/reports, graph nodes on their source
target, or the saved paper introduction. Drag, pasted reference links and the
project picker create read-only copies through each source owner at admission.
Missing sources reject the turn; later edits/deletion cannot change staged bytes.
They share attachment caps and add no roots or target authority. Prompts list
them separately from uploads and editable comments. Only ordinary node/project
Discuss/Work admits them; steering and other run kinds refuse them, and question
or watcher follow-ups clear them.

Answer-text selections offer a floating Comment action without disturbing Copy.
Overshoots clamp to answer text; a visible Comment command provides a keyboard
text-control alternative. The comment editor stays within the keyboard-adjusted
viewport at every width. Multiple editable/removable annotations remain per-chat
drafts with a count; blank comments block send until completed or removed.
Sending appends copied text and `comment: <comment>` to the human message, with
no source ids, offsets or durable annotation model. Acceptance clears drafts;
refusal retains them. [Artifact comments](paper-artifacts-and-result-views.md)
use their own viewer flow.

## Common episode parent

Auto-research and Experiment-loop are modes of one persisted episode parent.
The parent owns identity, human authorizer, graph target, lifecycle, durable
ending, native-session binding, operational ceiling, Stop state, report state,
and restart reconciliation. Mode adapters own their distinct admission,
authority, watcher/child settlement, and compact wrap-up facts.

### Episode isolation

Run captures separate code and graph isolation choices. Auto-research locks graph
isolation on and defaults eligible code isolation on; Experiment defaults both
off. Explicit ineligible code isolation is refused. Choices become immutable
at first launch.

Graph-isolated Experiment Run creates a branch from an immutable main head and
starts fresh. Otherwise it keeps its target; an Experiment on an existing branch
keeps that branch's isolation owner. Pending main observations stay on main.
Branch Work gains no Decision exception.

The first isolating episode records `EpisodeIsolation` before launch: owner,
optional graph branch, and worktree repository alias, machine/host, shared and
worktree paths, Git common directory, branch, starting branch/commit. Operation
state is separate. Children, Add N turns and human Experiments on its branch use
`isolation_owner_episode_id`, never a second binding, and must match its host.
Truth membership is unchanged.

Code isolation requires one run-scope repository and Git 2.38+ on the execution
host. A grant covering the shared checkout makes it ineligible. Every launch
rechecks grants and binding; Work/orchestration write the worktree, not the shared
checkout. Resume, Retry and restart retain it; missing/moved worktrees fail.

Human Merge reserves the isolation owner across all admissions until landing,
verification and cleanup finish. Live turns must settle. The
[compute gate](compute-jobs.md#episode-merge-and-removal-gate) owns job checks and
confirmation. Scheduler work without helper rows is represented by active or
degraded external watchers, or stopped watchers without completion; graph
conditions do not block removal. Confirmed jobs reach the merge agent. Unfinished
jobs leave `worktree_kept` with worktree, code branch and graph branch intact for
a later Merge; confirmed standalone removal may remove the worktree.

Episode Merge may commit leftovers respecting Gitignore, but refuses interrupted
Git operations, unmerged entries and dirty submodules. With no code conflict
and nothing for the agent to answer, RCP lands code locally and commits one graph
transition without a provider turn; otherwise one merge agent lands code and
answers the graph plan before RCP builds and commits the graph change.
[Episode merge](auto-research-and-branch-merge.md#episode-code-merge-and-cleanup)
owns landing and cleanup.

Every episode turn uses the parent's recorded human authorizer. A requesting
member owns an idempotent recovery receipt but cannot replace that authorizer.
Missing authorization refuses recovery and requires fresh human Run.

The validated session binding pins provider, session, host, reusable stage,
project, graph target and actor conversation. Human Run creates all three fresh:
episode, session and conversation; a chat id with turns is refused. Wakes retain
the recorded conversation. Provider switch activates only after mechanical
success and is offered only where a clean-session path exists: not child Work
or stopping episodes. Orchestrator wakes may switch and retain their paid
delivery. Its children without an explicit pick still use Settings' node_chat
profile. Switch requires a changed binding, including machine when movable;
plain Retry remains separate.

Only operational turns spend the ceiling. Validation, same-invocation repair,
exact Resume/Retry and hidden reports spend no extra unit.

### Episode archive

Project members may archive/unarchive any episode, including live or reporting
ones. The shared choice records actor/time separately from the authorizer and
changes presentation only. Work, watchers, reports, budgets and graph history
continue; lifecycle changes do not undo it. Children archive independently and
new episodes start unarchived. Restart, backup and transfer retain the choice.

`POST /api/projects/{project_id}/episodes/{episode_id}/archive` accepts exactly
`{"archived": true}` or `{"archived": false}`. Membership and write admission are
checked atomically with the mutation. Projections supply `archived` and
`can_archive`; lists expose **Show archived**.

## Experiment readiness and budget

Experiment starts are per exact graph target; loops elsewhere are informational.
Admission requires decided `governed_by` Decisions with selected options and no
pending Proposals, no open `blocked_by` Blocker, a nonterminal Experiment, and no
live parent, unresolved automatic invocation or deliverable watcher on this
target. A parent that armed no watcher and took no exit still blocks Run;
**Stop loop** releases it even when the loop looks inactive.

Readiness separates graph and operational reasons and adds
`isolated_start_reasons` for a prospective branch. Run waits while other-target
loops load; failed/partial reads report unknown overlap but leave Run available.
Every Experiment-node chat/wake receives this target's loop status, authorizer,
Stop attribution, other-target loops and refreshed watcher-state file, without
maintenance authority. Agents able to ask should clarify interference with other
branches; children pause conflicting work and tell their orchestrator. Branch
turns also receive read-only main graph and shared-checkout/Git pointers.

A completed, abandoned or superseded Experiment cannot start fresh until an
admitted graph change reopens it. This does not revoke an existing episode turn.
The control says **Start episode**, then **Start new episode** once history exists.
Run defaults to the node's `invocation_ceiling`; a human override pins only the
episode without changing graph state. A terminal episode with no live turn may
**Add N turns**, creating a continuation on the same node, target and session via
`continues_episode_id`. The inspector starts against the node's limit; history
keeps pinned usage while **Next episode limit** shows the current node value.

Fresh human Run may pick provider/model/reasoning, defaulting to node_chat;
machine stays with that profile. Continuations keep the source binding. Children
use the orchestrator's explicit pick, otherwise Settings. Starts and
reauthorization do not gate on compute readiness; [compute jobs](compute-jobs.md)
owns probes and cancellation.

Run creates no ExperimentAttempt: attempts are semantic bookkeeping, not budget
or admission. Nonblank initial goals remain exact; blank input gets a fallback.
Only the newest unresolved task in the newest same-target episode may Resume or
Retry operationally. Patch-only repair may reflect completed work, never repeat
effects or reopen an old episode.

## Experiment-loop graph authority

Each invocation receives focused Experiment authority and a compact control file:
phase, episode/target, counts, pinned Decisions, drift, completion criteria and
delivered watchers. Watcher state is separate; no prior transcript is supplied.
[Continuation prompts](providers-and-containment.md#continuation-prompts) owns
wake/recovery deltas, master pointers and report revocation. Current inline
restrictions preserve the objective, attempt ledger and completed progress.

Between Experiment turns a human may send an ordinary chat turn on its session
from Agents. It spends no episode budget and changes no Stop state. When another
owner's master or a report/revoking edit displaced its contract, the next owner
reopens its own master: chat for human fresh/recovery turns, episode lineage for
Experiment turns and renderer-free repair. Revocation clears only after that
owner succeeds. A chat-master Discuss edit revokes nothing.

Chats, invocations, wakes, recovery, reports and artifact edits reserve the exact
session/stage atomically. Busy artifact edit admission returns 409, with no
queueing, steering, budget spend or Stop change.

Experiment Patch may update its own attempt/status/guidance, create Evidence and
Blockers, assert legal epistemic/output edges and permitted Proposals inside its
pinned upstream/tested boundary. It cannot set standing, decide Decisions,
transition Hypotheses, change the pinned Decision bundle or remove nodes.
Experiment status does not control invocation. Validation, Apply and every child
context, correction, watcher, settlement and report retain the exact graph target.

## Graceful Stop and recovery

**Stop loop** persists intent before returning and before an unclaimed compatible
watcher can win a new claim. It means: finish the already-authorized turn, retain
its valid Patch and semantic result, stop existing and newly emitted compatible
watchers, and admit no automatic continuation. A later display-read failure
returns the accepted Stop with operational state and unavailable graph readiness;
it does not turn the durable Stop into a failed request.

The current loop's Stop also retires unclaimed observations adopted from older
episodes on the same Experiment, graph target, and execution host. Their origin
remains intact, but a later episode cannot revive their delivery. Reconciliation
of a historical Stop never retires a replacement episode's observations.

If no live task remains, Stop settles immediately. While the current turn is
queued, running, or pausing, Runs shows **Stopping gracefully** and recommends
waiting. A turn that is paused, failed, or interrupted has nothing in flight to
finish, and nobody resumes a turn they asked to stop: Stop durably abandons its
recovery, keeps its history, and settles. An Auto-research orchestrator's Stop
of a child that hit a provider limit therefore frees the Experiment at once.

Stop and pause do not cancel compute jobs. Stop does not cancel external work,
delete watcher history, edit Experiment status, create or close an attempt, or
discard a valid Patch.

Budget exhaustion starts no automatic wake. Pending completion remains visible
and unconsumed. Once the final operational turn settles, non-Stop endings enter
wrap-up; a later human **Add N turns** continues the same session with a new
ceiling and may claim retained compatible completion as its invocation one, and
**Start new episode** remains the fresh start.

## Watcher resources

Conversation, Experiment, and Auto-research child Work watcher targets are
separate exact resources, not a client-chosen mode field.

- A conversation `watch.json` wakes that same conversation.
- An Experiment watcher resource is keyed by project, exact graph target,
  Experiment node, and compatible episode, and wakes that bounded episode.
- A child Work watcher retains the parent episode and child route worker id and
  wakes that same route and native session, never the Auto-research root.

A branch watcher can never wake a main task, and a main watcher can never spend
a branch episode. Watcher selection, staging, atomic claim, task creation, and
episode association retain the same target.

An Experiment episode adopts unclaimed compatible observations from earlier
episodes of that Experiment on the same exact graph target and execution host.
Completions may coalesce across those origin episodes, including on a branch.
The receiving episode supplies the human authorization, native session, stage,
pinned Experiment policy (including workflow and skill packages), and invocation
budget; watcher origin is immutable provenance, including when another human
created or maintained the observation.
This adoption does not change ordinary conversation, Auto-research root, or
child Work routing. Existing group readiness and atomic, once-only claims still
apply. A child Experiment also retains its receiving parent's admission and
shared Experiment allowance gates.

Every watcher file has two all-or-none lists:

- `external` observations with required literal `check_command`, absolute
  `log_path`, and absolute `cwd`, plus optional nonblank `cancel_command`; and
- `graph` conditions from a closed vocabulary.

This is one external watcher form for direct scheduler submissions and
helper-launched processes. Experiment items may additionally carry their
existing `group` label; ordinary conversation items cannot. A helper's returned
watcher object uses these same fields; there is no job-id observer form.

The graph vocabulary is exactly: a named node reaching one of named statuses,
or a named Proposal being resolved after arming. There is no arbitrary query,
standing predicate, new-node arrival, or relation predicate.

## Graph-condition delivery

Graph conditions evaluate at accepted revision boundaries and at startup, using
the exact target's canonical transition order. The startup sweep runs on the
watcher retry worker immediately after the API is ready, never on the readiness
path; a remote read that fails there retries with the ordinary poll-pass
backoff. A staged draft never fires them.
Halted/degraded replay means not yet for that target; other targets still
reconcile, and their callbacks cannot clear a pending transient retry elsewhere.
A node removed after arming retires its condition.

Each condition stores its arming head. A node status already true at that head
is immediately ready; Proposal resolution is prospective and requires the
specific later resolution event. A resolved Blocker transition is observed from
the retained final node/event, with no delete operation.

Every graph wake spends one permitted invocation, including one caused by a
human Sync. External and graph completions that become ready together coalesce
into one target-consistent wake. Canonical event watermarks make crash/restart
delivery idempotent.

## External observation

Shell checks run in a cold login shell with a hard timeout. A timeout kills
the check's process group on its execution machine, including shell children;
it never cancels the separate external job being observed. SSH checks carry
their own bounded timeout owner so a lost client cannot abandon the check.
Exit `0` means the named work is gone, `1` means still present, and any other result is
unobservable. Active observations use the normal interval; repeated failures
persist bounded exponential backoff and identity jitter. Only exit `1` resets
the error count. A degraded observation is never inferred complete or dead.

Shell completion reports operational liveness, not scientific success. The
Experiment watcher-state file and generic Work wake message, including child
Work, retain the shell watcher's log-path evidence and ordinary coalescing and
claim path. The [compute jobs spec](compute-jobs.md) owns direct scheduler
submission, generic helper launch, and human cancellation.

A human may also retire one live Experiment observer directly once its episode
carries a durable ending. While the episode can still take a graceful **Stop
loop** that control owns its watchers; after the ending fence Stop is refused, so
the observer would otherwise keep the loop shut with no control but Cancel.
Retiring is not cancelling: the observed job keeps running, and the retired
observer can no longer deliver a retained completion. It is offered for one lone
external observation on the graph target being shown: a grouped member cannot be
retired alone, because a human-stopped member makes its whole group
undeliverable; a completed observation is a retained result to claim rather than
an observer to retire; and an Experiment graph condition runs no job, so this
control is not its answer. A live condition can therefore still hold an ended
episode shut.

An optional saved `cancel_command` runs only on a human Cancel request, after a
fresh check confirms active work. It uses the recorded execution host and cwd,
and never changes the watcher into a separate cancelled lifecycle. A successful
command is an attributed request; observation still determines completion.
Stop fences continuation and leaves the action available for still-live work.
Cancellation failures permit explicit retry. Transfer and offline restore
remove executable actions; ordinary restart preserves them.

Prompt guidance lets short jobs finish inline and recommends a watcher for
roughly more than ten minutes of waiting. This is not a runtime cutoff.

Experiment watchers may form immutable groups of at least two new observations.
A group wakes once when no member remains active and every nonretired member is
either complete or persistently unobservable at the capped tier. The latter is
diagnostic readiness, not scientific success. Stop/disposition items may retire
any staged compatible watcher after the agent has settled its work, external
observer or graph condition alike, and always travel in the `external` list.
Retiring a condition discards only a future graph delivery, so the agent that
armed one withdraws it the same way it withdraws an observer; a watcher it has
stopped needing otherwise holds the loop open and spends an invocation when it
fires. A stop still never claims RCP cancelled the process.

Initial validation, grouping, retirement, replacement, and insert commit
atomically. One invalid item arms none. An Experiment observer repeating an
ungrouped one already armed for that node, graph target, execution host,
directory, check command, and log path is one such invalid item: identity jitter
keeps the pair out of a shared delivery pass, so each spends a wake on one
completion. An unnotified completion counts as already armed, being a wake the
episode has not spent yet, and grouping exempts neither side: one group wakes
once, but two are two delivery units that coalesce only when they become ready
in a single poll. Retiring one member does not strand its siblings, because a
group's readiness ignores agent-retired members. Generic Work and child Work
arming is unchanged. Observing one
job through genuinely different commands cannot be told apart mechanically, so
the staged watcher state is what every surface that arms an Experiment observer
reconciles against first. An empty final watcher declaration is
legal with a success, Proposal, or Blocker Patch exit, or an open human question
or undelivered answer belonging to that human-started episode. A question never
excuses unwatched compute. An answer wake spends one normal Experiment invocation
and preserves the session, scope, target, Stop fence and ceiling; exhaustion needs
human reauthorization. Continuation episodes reopen their predecessor's questions
without changing origin provenance; ended episodes withdraw their cards. In the
continuation's creation transaction, unreceived, unclaimed answers are claimed by
invocation 1 when its binding passes the answer-wake origin checks. Its first
question snapshot carries those answers without spending another invocation.
The human-started continuation keeps its own prompt text and message identity;
claimed answers retain their separate, stable question-answer message identities
and ordinary projection retries.
If the saved origin authority or matching execution binding cannot be proved,
the answered card remains read-only.
Missing or malformed
handoff enters same-session correction without spending another unit and may not
repeat operational work.

At Work, child Work, and Experiment-loop settlement, RCP also refreshes every
job launched by the turn. A still-running job absent from the declared job
observers is a correctable handoff defect listing the unobserved job ids. The same correction
round repairs it without another invocation; jobs already exited need no
observer, and a turn that launched nothing is unaffected. Child Work uses the
ordinary Work reader, validator, and arming path for its final `watch.json`.

A child watcher claim creates one continuation and spends one parent B unit
atomically. It requires a running parent, a route without a Stop fence, and a
succeeded current child task. A repeated delivery cannot create a second wake;
exhausted B leaves the completion pending. A succeeded child with an armed,
undelivered watcher is waiting, which blocks the root's guarded finish. Episode
Stop and the root's child `stop` verb retire the child's watchers within their
admission fence. They never cancel the observed jobs.

## Watcher maintenance authority

An authorized node Work chat may maintain only its focused Experiment resource;
authorized project Work may maintain live Experiment resources in that project
and exact graph target. Discuss may inspect staged state but cannot mutate it.
Origin chat, provider, path, or maintenance machine grants no authority.

Maintenance uses its own Work task/session, spends no Experiment invocation,
does not create an attempt, and never replaces the episode's native-session
binding. Stop, watcher claim, and competing maintenance have one atomic winner.

A Work turn is not an episode turn, even on the episode's own session, so its
maintenance cannot end the episode. A file that stops watchers and arms none is
refused when, after its stops, the episode has no pending turn, no live watcher,
and no undelivered completion. The refusal rolls back. Its diagnostic states
that episode state and enters the ordinary maintenance correction round. The
agent either drops a stop, so that watcher still wakes the loop, or arms an
observer for replacement work it already launched. Completion and authority
pauses remain the episode turn's Patch exits, recorded when a watcher wakes it,
and a human **Stop loop** remains the other way to end the loop.

## Live artifact reconciliation

The existing background reconciliation pass refreshes helper-job state and
captures eligible final live-artifact snapshots, including versions whose
watched episode ended without a job. Capture runs without a viewer. An
incomplete read retains a diagnostic and a persisted retry deadline; an outage
never becomes a complete final result. Capture changes no episode verdict,
budget, Stop fence, or graph state.

## Visual wrap-up

Completion, budget exhaustion, unrecoverable failure and authority pause fence
operational work and enter one hidden report allocation. Explicit Stop declines
it. An ending with no provider session terminalizes directly without a report
record or error.

The report resumes the exact session/stage with the durable ending, report
skill/output pointer and immutable compact mode receipt, never a rebuilt graph
or transcript. Receipt compaction follows a fixed order and storage-encoded size
bound. Permanent admission defects record one failed wrap-up and terminalize;
transient locks/outages retry. Existing rows reuse their receipt; warnings are
once per process, episode and failure kind. At most three provider attempts clear
the exact output before each run and spend no operational budget.

Startup requeues an interrupted hidden allocation rather than creating another.
It clears the old scope fingerprint and records a private dispatch-reset fence
newer than prior attempt receipts. Only that fence authorizes redispatch; old
attempts remain history.

Valid `episode-report.html` becomes an Artifact whose lifecycle binds its
original version in the opaque sandbox. It has no graph, Proposal, watcher
or command channel and cannot change the ending. Final report failure is visible
beside the ending, nonblocking, with no manual report Retry; it never becomes
episode health, a recommendation or a reason to withhold controls.

A member may delete a report once its wrap-up is ready. The report row stays,
marked deleted, as lineage: the episode shows no report and recommends nothing
about it, and the lifecycle never writes another. Delete waits while the wrap-up
is still publishing, because the report row commits with the ready wrap-up.

**Why:** a ready wrap-up without its report row reads as broken history to
transfer and would let the report lifecycle run again.

## Runs projection

The backend publishes one health, Runs section, recommended next step and exact
controls from episode, task, Stop, budget, watcher, report and node state. The
ending fence decides whether the episode is over. A terminal episode on a closed
Experiment reads Completed even after human Stop, whose ending remains history.
The browser neither rebuilds health from task rows nor suggests unavailable actions.

Report availability is independent of current episode: the newest surviving
report for that Experiment and target remains linked to its owner. `report_is_current`
selects **Open report** or **Previous episode report**.

Active observers show **Waiting on watchers**; completed observations show
**Completion pending delivery**. A receiving Auto-research parent unable to admit
work or out of shared Experiment allowance shows **Needs action** with its current
reason. Parent facts are read live, not saved as delivery errors.

Runtime, parent, tasks, usage and report come from one SQLite read snapshot.
Recovery controls address only its exact current operation; missing rows offer
none. Admission rejects older attempts or overlapping episode work. Accepted
recovery refreshes the authoritative projection before releasing busy controls;
refresh errors stay separate from admission errors.

Details retain target, history, pinned budgets, next limit, guidance validity,
watcher provenance/groups, session continuity, diagnostics and report.
Ordinary chats and Paper stay outside Runs.

### Episode browser preference

Each bounded episode stores one `browser_requested` launch preference. Its
Experiment projection and API response carry that value. Human Continue keeps
the source episode's preference. Retry, watcher wakes, queued follow-ups, and
bounded Experiment turns preserve the admitted preference and stable stage.
Watcher continuation policy includes the preference.

Each turn exposes a durable browser status: `not_requested`, `granted`,
`unavailable`, or `lost`. A browser failure does not fail the provider task.
