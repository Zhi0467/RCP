# Graph consolidation and memory

This spec owns nightly consolidation, its schedule, Inbox attention, and
shared operational memory. The implementation owners are
[the scheduler](../../src/rcp/consolidation.py),
[run storage](../../src/rcp/storage/consolidation.py), and
[the official workflow](../../src/rcp/skills/workflows/graph-consolidation/WORKFLOW.md).

## Authorization and schedule

A member enables consolidation with a local time and time zone. Each choice
captures the acting member and a fresh authorization id, even if the choice is
unchanged. The authorization expires after `CONSOLIDATION_AUTHORIZATION_DAYS`.
Runs that have not launched under the old id fail. Any member may disable
consolidation. Expiry remains visible until renewal or disablement.

One schedule covers main and every non-archived branch, including branches with
running Auto-research. Each target gets one occurrence per local date, its own
due time and covered head. A nonexistent daylight-saving time uses the next
valid minute. An ambiguous time uses the first instance.
Polling follows the machine wake delay and `CONSOLIDATION_POLL_SECONDS`.
Occurrence handling, the next due time, and run creation commit together.
Missed dates collapse into one occurrence per target.
An unresolved run leaves that target's occurrence owed without blocking others.

Expired authorization starts nothing. Departed authorizers, refused work, paused
resumable chat, unavailable history, or unresolved launch configuration produce
failure rows. They advance the occurrence, never coverage. If no non-consolidation
revision followed the covered head, the occurrence records `skipped`. Commits
of a run that failed count as non-consolidation, so the next night checks them. A branch's
initial coverage is its immutable fork revision, so inherited work alone does
not trigger it. Otherwise one turn is admitted on that target.

Only success advances coverage. It covers the input head and subsequent revisions
from that operation, never concurrent work. Immediately before the first launch,
RCP rechecks the authorization id, expiry, membership, and write admission.
This includes release after sign-in and startup recovery.
A queued turn whose stored row fails launch validation fails, so its run settles
as a failure row and the target's next occurrence is free. Any other launch error
leaves the turn queued for the next poll and adds a warning event to the turn
whenever the error changes, not on every poll.
Started turns settle under their captured authority. Apply still checks live
membership.

## The consolidation turn

Each target receives one ordinary Work turn with its graph target, `work_auto`,
schedule trigger, and authorizer. Only the server-owned run binding grants
consolidation behavior. A chat id or request field cannot grant it. Each target has a
dedicated chat and a fresh native session each night, with ordinary scratch and
artifacts. Branch ownership grants no orchestrator authority. The official
workflow and dependencies resolve through the skill registry independently of
project defaults and appear in the launch receipt.

The command client offers `validate`, keyed `apply`, `flag`, `bulletin` (a run with
no episode always holds it), and the graph
history reads when a read scope exists.
It refuses watcher handoff. Apply persists immutable bytes and their digest under
the operation and key. It returns the committed revision.
Identical retries replay. Changed bytes under the same key are refused.
Another key with the same bytes shares the effect.
A matching final `patch.json` is not applied twice.
New final bytes use ordinary Work Apply and correction.

The charter is “Tidy and check sources; do not run experiments.” The turn rewrites
unreadable asserted nodes, defines shared terms, and directly merges, supersedes,
or removes obsolete or duplicate Evidence. Suggestions hold only work needing a
human. Each asserted Experiment gets a design check. It must describe one study,
with a concrete matrix of operations and runs, measurements, defined internal
names, and consistent fields. An accepted Experiment is rewritten only when its
operation is unreadable. This returns it to asserted standing.
Other rewrites of accepted nodes are suggestions.
Existing protected beliefs require
[Proposals](authority-and-proposals.md#protected-belief-proposals).

Historical links are re-homed with ordinary verbs only when sources support the
replacement. A Question's Decision connects to existing Hypotheses it serves or
remains a pure Experiment input through `governed_by`.
No Hypothesis is invented for topology.
Blockers linked to Questions or Decisions move to the Experiments they prevent.
Where sources support it, `requires_decision` wrappers become
genuine Experiment inputs. Otherwise, retain the edge and suggest its id.
Blocker resolution remains explicit. Ballots stay unchanged.
An upgrade alone triggers no cleanup.

Fact-check Evidence changed since coverage against full cited artifacts, job
logs, repository files, papers, and web pages. Do not use conversation transcripts.
Provider-native subagents may divide audit, fact-checking, and literature search.
The root owns integration, Patch, Apply, and report. Source-supported corrections
go directly into the Patch. Unsettled findings use `overclaim`, `underclaim`,
`no_source` (the sources read lack the claim), or `unverified` (the named source
was not read). The workflow suggests at most five flags per night.
This is not an enforced cap.
A flag never changes the claim, validity, or standing itself.

The same turn searches recent literature bearing on open questions and claims,
recording the query, date, source, and whether it accessed the full text or abstract.
Prefer edits and Suggestions over new nodes. Ordinary Evidence-edge qualifications
remove and recreate the edge. Protected belief edits need Proposals.
Consolidation cannot file merge-only atomic protected-edge replacements.
Use safe, separately approved steps or suggest them.
Consolidation may add source-grounded external-publication Evidence with assessed
edges. It fact-checks its additions after Apply.

Finish with `consolidation-report.html` in the ordinary artifact root, listing
flags and committed changes. Its Literature section names changes, papers,
searches, access gaps, and when nothing new was found.
**Why:** expiring authorization bounds nightly maintenance to one invocation per
active target. Ordinary Work preserves existing authority and merge rules.
In-turn Apply lets the report describe committed work.

## Outcomes and Inbox rows

The outcome is distinct from the task verdict. Settlement reconciles the run,
ended task, canonical operation commits on its target, and captured artifact.
Success requires a succeeded task and a viewable HTML report.
It also requires that no graph update was rejected or unavailable.
Startup and scheduling retry settlement. Settled outcomes are final.
Later history recovery verifies revisions and counts. It cannot turn failure into
success or coverage. Paused, stopped, abandoned, or irrecoverably interrupted runs
fail without coverage. They cannot resume as scheduled runs.
Humans may start separate Work in the chat.

Each run creates one target-labelled Consolidation Inbox row. Report rows offer
Keep or Dismiss, protecting temporary artifacts from expiry until settled. Viewer
Keep closes the row. A Bulletin link retains the report but leaves review open.
Failure rows offer a typed error and Dismiss. Failed tasks with no recorded
provider launch, including invalid persisted requests, have verified empty history
and expose no operation or chat link. Known refusal codes remain specific; other
prelaunch failures use `consolidation_launch_failed` with the original diagnostic.
Postlaunch failures list canonical revisions.
Unreadable revisions are marked unverified, rather than falsely shown as empty.
Both kinds of failure show the count of created Proposals.

Keep or Dismiss closes the row for all members using `can_write` independently
of graph availability. `kept_at` is distinct from expiry protection.
Proposals retain the ordinary Inbox path. Each row emits a target-labelled
notification. The first observation of authorization near expiry or already
expired also emits a notification. Closing a row, renewing a grant, or deleting
a grant invalidates its notices.

## Evidence to check

Flags are SQLite attention records, never graph state. Only a consolidation
mailbox can author the finding kinds.
[Flag commands](../../src/rcp/runs/evidence_flag_commands.py) and [flag
storage](../../src/rcp/storage/evidence_flags.py) own schemas, bounds,
source syntax, and keyed receipts. The id must name current, non-retired Evidence
on the run's target. Creation checks live authorizer membership transactionally.
Identical keys replay. Changed arguments are refused.
Only one open or addressing flag is allowed per project, target, Evidence, and kind.
Duplicates return the existing flag.

After every successful or replayed Apply, keyed or final, and at settlement, RCP
raises idempotent `consolidation_added` notices for current Evidence that the run's
canonical Patches on its target created, not a graph diff. Agent keys cannot use
the reserved `rcp:` prefix or raise this kind. Failure to record a hold produces a
warning without undoing Apply. If the run's canonical Patches cannot be read,
settlement waits. If history still cannot be verified at settlement, the run fails
as `history_unavailable` without coverage.

The selected target's open or addressing flags appear in Inbox.
`consolidation_added` and `unverified` are notice-only. They offer no Address action
and cannot join a batch. The endpoint refuses them.
Other kinds, including `no_source`, offer ordinary member-authorized Work on that
Evidence and target. They use node-chat defaults and source-bounded instructions
to correct only supported claims and ask when unsure. Flags grant
no authority. Replays return the same chat and turn.
A flag that is already being addressed also returns that chat and turn.
Address keeps the member in Inbox. Open chat enters the conversation.

With at least two addressable findings, Address all submits the oldest bounded
batch. [API request types](../../src/rcp/api/consolidation.py) own the bound and
reject empty or duplicate ids, non-open flags, notice-only kinds, mixed targets,
and absent or retired Evidence. One finding uses node Work.
Multiple findings share one project Work turn.
Sorted locks and one task transaction bind all flags or none.
The request key and exact ids survive a 5xx or network failure and replay their
own turn after settlement; a refusal lets Retry pick a fresh batch. Every still-bound flag follows batch settlement and recovery.

A succeeded Address turn with outcome `done` closes its flags if no graph update
was rejected or unavailable. A partial or could-not outcome, failure, interruption,
or Stop reopens the flags and retains the chat. Another Address starts a new turn
in a new chat and replaces the flag's chat link.
Resume or Retry returns the flags to addressing unless they were dismissed or
rebound. Startup follows the latest recovery bindings and settles ended turns.
It adapts older request receipts. Dismiss closes flags for all members.
Admission rechecks current Evidence. Like node chat, it does not fence a concurrent
graph transition.
Visual states, source links, and phone controls belong to
[the interface spec](interface-and-visual-design.md#evidence-to-check).

## Memory

Project memory is Markdown on the project's base, outside Git, owned by
[`memory_files.py`](../../src/rcp/agents/memory_files.py). The local root is
`<data_dir>/memory/<project_id>/`; the remote root is the base account's
`~/.rcp/memory/<project_id>/`. Launch creates it on demand using the existing
remote transport where needed. Shared notes live in `shared/notes/`; the index
is `shared/MEMORY.md`. Each index line links a note and gives a one-line hook.
Notes begin with `# Title`. Member folder paths live under `members/<user_id>/`;
private thread integration owns their admission.

Ordinary project Work and Discuss chats read and write `shared/`. Experiment,
Auto-research roots and workers read and add notes; retaining existing notes is
a prompt rule. Consolidation reads, edits, merges and prunes shared files,
reports each change, and never touches member folders. Seed, Refresh, branch
merge, Paper coach and artifact edit receive no memory access. Memory has no
graph, replay, merge or paper authority.

Launch prompts point to the live index and label it as notes, never instructions
or authority. `MEMORY_INDEX_MAX_BYTES` bounds the recommended index size, not
note storage. Launch stats the index; an oversized index prompts the agent to
read its start and prune it; add-only runs request consolidation pruning. There
is no staged lesson snapshot or lesson command.

On the first launch build per project, the backend migrates all legacy
`operational_lessons` rows into `shared/notes/lesson-<lesson_id>.md`, with
`origin: lesson <lesson_id>` on the second line. Create-if-absent preserves files
after an interrupted migration. The index orders human-owned notes first and
newest first within each group. Only after writes complete does the backend
record `memory_migrations`; an unreachable base defers completion. Legacy lesson
and command receipt tables remain read-only migration sources. Settings and
WebMCP no longer expose lessons; agents maintain memory through file edits.

## Transfer, backup, and restore

Memory folders are outside SQLite backup and project transfer. A transfer to the
same base and account finds the existing folder by project id. Note authorship
is not tracked after migration. Legacy lesson rows, flags, schedules, per-target
coverage, runs and receipts remain space-local; SQLite backup captures them.
Restore retains legacy lessons, coverage and closed rows, fails unresolved runs
on every target, and deletes consolidation schedules until a member configures them again.

## Private agent schedules

Private threads expose `schedule add`, `list`, and `cancel`. Add requires the
persisted admitting member message from the composer, voice, or handoff, with a
human trigger and no question-follow-up binding; wakes cannot create schedules.
The agent creates one only when asked. Mutations carry an idempotency key, and
`AGENT_SCHEDULES_MAX_ACTIVE` bounds active schedules per thread.

A schedule names an IANA timezone and either a local `YYYY-MM-DDTHH:MM` instant
or five numeric cron fields with wildcards, lists, ranges, and steps. Sunday is
0 or 7. When both day-of-month and weekday are restricted, either matches.
Local times use consolidation's first-fold and next-valid-gap-minute rules.
The admitting turn's timezone is the default; without it the agent must supply
one. Invalid and past one-shot times are refused.

The consolidation poller's second dispatch step advances due schedules and
preserves their first pending occurrence in one transaction. One-shot schedules
become done. Busy, queued, and paused threads retain pending notes. Once eligible,
one atomic admission claims all observed notes for one Work wake, listing each
note and its missed fire count. Admission retains the thread's latest provider,
native session, scratch, and member binding and rechecks the ordinary project
write fence and session constraints. Lost thread access disables pending schedules.

Schedule controls are owner-only. Restore keeps schedules disabled with reason
`restore`, clears their pending wakes, and requires explicit re-enabling. A past
one-shot cannot be re-enabled. Consolidation's separate restore behavior remains
delete-on-restore.
