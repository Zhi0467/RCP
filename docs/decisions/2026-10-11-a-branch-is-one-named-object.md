# A branch is one named object

Confirmed by the human 2026-10-11.

## Decision

A graph branch is one object with a name. Every screen reads that name and
nothing else: no instruction text, no node title, no id prefix.

One branch picker, on the view-tabs strip, selects the branch for the whole
project. Overview, Inbox, Research, and Agents show only the selected branch.
Runs and Artifacts stay project-wide and tag each item with its branch name.

Auto-research runs and Experiments create, name, and archive branches through
one flow and one owner. A branch is still created only by an episode, and
`branch_id == episode_id` stays.

A rename changes a label, not the graph. Names are not Patch history, and any
member who can open the project may rename or archive a branch.

## Why

The branch never became a first-class object, so each surface invented its
own version of it. The picker labelled branches by instruction text or an id
prefix, Agents by an id prefix with its own graph filter, Artifacts only by
"Auto-research" or "Experiment", and the picker took a third strip of screen.
In a real project with eight branches nobody could tell them apart, and a
second graph selector inside Agents disagreed with the first.

## Consequences

- An unnamed branch shows `research-<mon>-<day>` or `experiment-<mon>-<day>`,
  with `-2`, `-3` on the same day, so old branches need no migration.
- Names are unique within a project and never reused, so a tag on an old Run or
  Artifact keeps meaning one branch.
- Agents loses its graph filter and per-card branch and role tags.
