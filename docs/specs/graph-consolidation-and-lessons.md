# Graph consolidation and lessons

This specification owns the nightly graph consolidation, its authorization and
schedule, its Inbox rows, and the per-project store of operational lessons.

## Authorization and schedule

A project member enables consolidation in Project settings by choosing a local
time and time zone. The schedule records that member, the time of the choice,
and a fresh authorization id, and it expires
`CONSOLIDATION_AUTHORIZATION_DAYS` later. Choosing again, with the same or an
edited time and zone, renews it under the
acting member with a new authorization id. A run admitted under the previous
authorization that has not launched yet ends as a failure; the next occurrence
runs under the new one. Any member can turn it off. An expired schedule
starts nothing and stays visible in the settings card until renewed or turned
off.

Each local calendar date of the schedule is one **occurrence**, due at the
chosen local time (a time that does not exist on a DST transition date is due at
the first valid minute after it; an ambiguous one at its first instance). The
consolidation scheduler checks due schedules every `CONSOLIDATION_POLL_SECONDS`
once the machine has been awake long enough for an automatic launch. Handling a
due occurrence and computing the next due occurrence commit in one transaction
with the run row it creates, so a restart neither loses nor repeats an
occurrence. The next due occurrence is the first one after the handling time, so
any number of missed occurrences collapses into one.

A due occurrence:

- stays owed, starting nothing, while an earlier consolidation run of the
  project is unresolved (admitted, queued, running, or awaiting its
  outcome);
- starts nothing when the schedule has expired;
- creates a failure row and starts nothing when the authorizer is no longer a
  project member or the project no longer accepts new work;
- creates a failure row without a task when the consolidation chat has a resumable
  paused turn (`consolidation_chat_paused`), history is unavailable
  (`history_unavailable`), or launch configuration cannot resolve
  (`consolidation_launch_unavailable`), advancing the occurrence while leaving the
  covered head unchanged;
- records the outcome `skipped`, remembered for the settings card's recent
  nights, and starts nothing when no revision outside consolidation runs has
  been committed on main since the last covered head;
- otherwise creates a run row and admits one consolidation turn bound to it.

The **covered head** advances only when a run succeeds: to the run's input head,
plus any later revisions committed by that run's own operation. A failed run
leaves it unchanged, so the next night tries again. A revision committed by
someone else during a run is never covered by that run.

Immediately before the provider first launches, including a launch released by
provider sign-in or startup recovery, RCP checks the captured authorization id
against the schedule, expiry, the authorizer's membership, and project write
admission. A failed check ends the run as a failure without a provider turn. A
turn that already started settles under its captured authority; every Apply
still checks live membership.

## The consolidation turn

The turn is an ordinary Work launch on main with the ordinary `work_auto`
capability, trigger `schedule`, and the schedule's authorizer as
`authorized_by`. Its graph authority equals ordinary Work: new
ResearchQuestions and Hypotheses may be created, existing ones change only by
Proposal, and accepted nodes are not removed directly. What sets it apart comes
only from its server-owned binding to a consolidation run, never from a chat id,
workflow, or request field.

It runs in the project's dedicated consolidation chat, admitted by the
scheduler with no native session, so it never reuses an earlier night's
provider transcript. The chat's stable scratch and artifact folders persist as
for any chat. It runs the official `graph-consolidation` workflow and its
dependencies, resolved through the skill registry regardless of project skill
defaults and recorded in the launch receipt.

Its staged command client offers exactly `validate`, keyed `apply`, and
`lesson`. It cannot hand off watchers: a `watch.json` it leaves is refused.
Ordinary Patch correction rounds apply.

Keyed `apply` commits a Patch on main inside the turn and returns the new
revision. RCP records an immutable copy and digest of each applied Patch under
the operation and key, with a source effect id derived from the operation and
Patch digest; repeating a key with the same bytes replays the result, and
different bytes under a used key are refused. The same bytes under another key share the same canonical effect.
At settlement RCP matches the final `patch.json` by operation and
digest against those receipts: a match is not applied again, and a new final
Patch applies as for any Work turn under its own effect id.

Besides consolidating, the workflow keeps the graph readable. It rewrites
`asserted` nodes that fall short of the "Writing for a reader" rules in the
graph rules every graph-writing launch receives, and it builds the glossary
from terms several nodes use without defining. It only suggests rewrites of
accepted nodes, because an edit would return them to `asserted`. A rewrite of
an existing ResearchQuestion or Hypothesis goes through a Proposal, like any
other change to a protected belief.

The workflow ends by writing `consolidation-report.html` in the turn's artifact
root, captured by ordinary turn-artifact discovery.

## Outcomes and Inbox rows

A run's outcome is separate from its task verdict. RCP settles it by run id from
the ended task, every canonical commit of the run's operation, and the
captured artifact, and retries settlement at startup and on scheduler passes
until it is recorded. A settled outcome is final; later history recovery can
verify its revisions and Proposal count but cannot turn a failure into a report
or advance its covered head. A run succeeds when the task succeeded, no graph
update of the turn remains rejected or unavailable, and the report was captured
as a viewable HTML artifact.

Paused, stopped, abandoned, or irrecoverably interrupted consolidation tasks
settle as failures without advancing the covered head and are never resumed,
retried, or continued as scheduled runs; a human may start a separate ordinary
Work turn in the same chat.

Each run leaves one row in the project's Inbox under **Consolidation**:

- a **report** row when the run succeeded. It opens the report in the artifact
  viewer and offers **Keep**, which keeps the artifact into the Artifacts
  panel, and **Dismiss**. While the row is open the report artifact does not
  expire. Dismiss gives it the ordinary temporary-artifact retention from that
  moment. Keeping the artifact from the viewer also closes the row as kept;
- a **failure** row otherwise, with a typed error. A failure before launch has
  no operation and lists no revisions. A failure after launch lists the
  revisions RCP committed for the operation, read from history; if history
  cannot be read the list is marked unverified rather than empty. It offers
  only **Dismiss**.

The consolidation response projects `can_write` from project write admission.
Keep and Dismiss use that offer independently of graph-history availability;
these actions never require a graph mutation. Artifact Keep availability and
kept state use `kept_at`, independently of temporary expiry protection.

Both rows show how many Proposals the run's commits created. Keep and Dismiss
close the row for every member. The rows grant no authority; Proposals appear
in the Inbox through their ordinary path.

Each row sends one `consolidation` notification, as does a schedule's
authorization when RCP first observes it within its last
`CONSOLIDATION_EXPIRY_NOTICE_DAYS` or already expired. A closed row or a
renewed or deleted authorization makes its pending notice ineligible.

## Operational lessons

A lesson is a short note on how to get work done in the project. Lessons are
RCP-owned records in the data directory, never graph state, and never authority:
a lesson cannot widen capability, write roots, graph target, or budget. Adding
one is an operational command, separate from filesystem and graph authority.

Owners with a served mutating command mailbox may add a lesson with `lesson
add`: human-triggered Work, child Work, Experiment-loop turns, the Auto-research
orchestrator and its workers, and the consolidation turn. Validation-only
clients (Discuss, Seed, Refresh, branch merge) do not. The consolidation turn
may also page through every lesson with `lesson list` and update or delete
agent-written ones. A lesson written or edited by a member is human-owned;
agents cannot update or delete it. Each agent mutation is keyed by operation,
subcommand, and key, checked against live membership of the task's authorizer,
and committed with its durable result in one transaction, so a retry replays
even after a delete. Members list, add, edit, and delete lessons in Project
settings.

Every Discuss, Work, Experiment-loop, and Auto-research launch, and the
consolidation turn, receives a pointer to one staged `lessons.md` rendered from
the store when the launch is staged, bounded by `LESSONS_RENDER_MAX_BYTES`,
human-owned lessons first, each group newest first, with the count of omitted
lessons stated. Text and count limits are `LESSON_TEXT_MAX_CHARS` and
`LESSONS_PER_PROJECT_MAX`.

## Transfer, backup, and restore

Lessons, schedules, runs, and their receipts stay with the space and are not
part of a project transfer. Backup captures them with the rest of SQLite.
Restore keeps lessons and closed rows, ends unresolved runs as failures, and
turns off every restored schedule until a member enables it again.
