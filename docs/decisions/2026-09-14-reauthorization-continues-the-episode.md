# Reauthorization continues the episode

Confirmed by the human 2026-09-14. This record reverses the sentence in
`docs/specs/auto-research-and-branch-merge.md`: "Reauthorization always creates
a new episode, native session, and branch; it never reopens an exhausted
parent."

## What happened

An Auto-research episode that spent its ceiling carried twenty graph revisions
on its branch and an orchestrator session that knew all of them. Reauthorization
as designed would have started a new episode on a new branch from `main` with a
fresh session, leaving that branch unmerged and that memory behind, and it was
only offered in one state the episode never reached. Experiment loops had a
different path, a Run of the node with a ceiling, so the same human act had two
shapes.

## The decision

Reauthorization adds turns to the same episode. Same episode id, same graph
branch, same native session. The ceiling rises by the number the human types;
the ending, its diagnostic, and any Stop request are cleared in the same
transaction; a lifecycle notice tells the orchestrator how many turns it now
has, and delivering it resumes the exact session. An existing report stays as
history and is superseded by the report of the next ending. Pending child
completions the ending had frozen become deliverable again. The same route and
the same control serve Auto-research and Experiment loops, on any ended or stuck
episode with no live turn.

## What this costs, stated plainly

An episode can end more than once, so "ended" is no longer final until the human
says so; every reader of `ending` must tolerate its clearing, and wrap-up state
resets while the report row is retained. The branch keeps growing until the human
merges it. A stuck episode is continued rather than replaced, so the continuation
must first settle whatever stuck it.
