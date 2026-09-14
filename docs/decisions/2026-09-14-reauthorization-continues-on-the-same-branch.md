# Reauthorization continues on the same branch

Confirmed by the human 2026-09-14, revised the same day after a design review.
This record reverses the sentence in `docs/specs/auto-research-and-branch-merge.md`:
"Reauthorization always creates a new episode, native session, and branch; it
never reopens an exhausted parent."

## What happened

An Auto-research episode that spent its ceiling carried twenty graph revisions
on its branch and an orchestrator session that knew all of them. Reauthorization
as designed would have started a new episode on a new branch from `main` with a
fresh session, leaving that branch unmerged and that memory behind, and it was
only offered in one state the episode never reached. Experiment loops had a
different path, a Run of the node with a ceiling, so the same human act had two
shapes.

The first revision of this decision reopened the same episode row. A review of
the persistence showed why that does not work: the wrap-up fence, the report
row, the report attempts, transfer records, watcher fences, and Experiment exit
receipts all assume one ending per episode id, and a delayed callback from the
first ending would land on the reopened one.

## The decision

Reauthorization creates a continuation episode. A new episode record, chained
to the ended one by `continues_episode_id`, on the same graph branch, with the
orchestrator resumed in its exact native session and told how many turns it now
has. The human's number is the continuation's ceiling. Child routes the ending
froze are carried over, so their pending completions become deliverable. The
ended episode stays terminal history with its receipt and report untouched. The
same route and control serve Auto-research and Experiment loops, on any episode
with a terminal status and no live turn, including a human-stopped one; stopped
watchers stay stopped. A continuation is a new human grant: it records the member
who made it, and the source keeps its own authorizer.

## What this costs, stated plainly

An episode chain is more than one row, so every surface that shows "the run"
shows the chain, and reports come one per ending. The branch keeps growing until
the human merges it. A stuck source must first settle to a terminal status, which
the reconciler now guarantees.
