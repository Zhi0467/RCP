# Graph consolidation and lessons

This specification owns the nightly graph consolidation, its authorization and
schedule, its Inbox rows, and the per-project store of operational lessons.

## Authorization and schedule

A project member enables consolidation in Project settings by choosing a local
time and time zone. The schedule records that member and the time of the
choice, and it expires `CONSOLIDATION_AUTHORIZATION_DAYS` later. Choosing again
renews it under the acting member. Any member can turn it off. An expired
schedule starts nothing and stays visible in the settings card until renewed or
turned off.

RCP's consolidation scheduler checks due schedules every
`CONSOLIDATION_POLL_SECONDS` once the machine has been awake long enough for an
automatic launch. A due schedule:

- starts nothing when it has expired, or when its authorizer is no longer a
  project member (which also records a failure row);
- starts nothing when main's head equals the head of the last consolidation
  that started, and records the outcome `skipped`;
- starts nothing while an earlier consolidation turn of the project is still
  running;
- otherwise starts one consolidation turn.

The next due time is the next occurrence of the local time after the moment the
schedule was handled, so any number of missed times collapses into one run.

## The consolidation turn

The turn is an ordinary-profile Work launch with the `consolidate` task
contract, trigger `schedule`, and the schedule's authorizer as `authorized_by`.
Its graph authority equals ordinary Work on main: new ResearchQuestions and
Hypotheses may be created, existing ones change only by Proposal, and accepted
nodes are not removed directly. Membership of the authorizer is checked again
at every Apply.

It runs in the project's dedicated consolidation chat with a fresh native
session each night; its only memory of earlier nights is the graph and the
lessons. It runs the pinned official `graph-consolidation` workflow regardless
of project skill defaults.

Its staged command client offers exactly `validate`, keyed `apply`, and
`lesson`. Keyed `apply` commits the Patch on main inside the turn and returns
the new revision, idempotently by key within the operation. A `patch.json`
consumed by `apply` is not applied again at settlement; an unconsumed one still
applies at settlement as for any Work turn.

The workflow ends by writing `consolidation-report.html` in the turn's artifact
root. RCP captures it as an ordinary temporary turn artifact.

## Inbox rows

Each consolidation turn that started leaves one row in the project's Inbox
under **Consolidation**:

- a **report** row when the turn succeeded and the report was captured. It opens
  the report in the artifact viewer and offers **Keep**, which keeps the
  artifact into the Artifacts panel, and **Dismiss**;
- a **failure** row otherwise, naming the failure and listing the revisions RCP
  committed for that operation, read from history rather than from the agent.
  It offers only **Dismiss**.

Both rows show the number of Proposals the turn created. Keep and Dismiss close
the row for every member. A dismissed report remains a temporary artifact of the
consolidation chat and expires with it. The rows grant no authority; Proposals
appear in the Inbox through their ordinary path.

Each row also sends one `consolidation` notification, as does a schedule
entering its last `CONSOLIDATION_EXPIRY_NOTICE_DAYS` before expiry.

## Operational lessons

A lesson is a short note on how to get work done in the project. Lessons are
RCP-owned records in the data directory, never graph state, and never authority:
a lesson cannot widen capability, write roots, graph target, or budget.

Any launch with a staged command client may add a lesson with `lesson add`. The
consolidation turn may also update or delete agent-written lessons. A lesson
written or edited by a member is human-owned; agents cannot update or delete
it. Members list, add, edit, and delete lessons in Project settings.

Every Discuss, Work, Experiment-loop, and Auto-research launch receives a pointer to one staged `lessons.md` rendered
from the store when the launch is staged, bounded by `LESSONS_RENDER_MAX_BYTES`,
human-owned lessons first, each group newest first, with the count of omitted
lessons stated. Text and count limits are `LESSON_TEXT_MAX_CHARS` and
`LESSONS_PER_PROJECT_MAX`.

Lessons travel with a project transfer. Schedules and consolidation rows do not:
an authorization belongs to a member of one space.
