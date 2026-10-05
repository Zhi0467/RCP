# Operational lessons live outside the graph

**Status:** accepted with the human on 2026-10-02. Implementation is tracked in
the [graph dreaming handoff](../handoffs/handoff-2026-10-03-graph-dreaming.md).

## Decision

Each project has one RCP-owned store of operational lessons in RCP's data
directory: short notes on how to get work done, such as how a cluster wants
jobs submitted or which library call misbehaves. An agent records a lesson
during any turn with a staged `lesson` command. The nightly consolidation turn
merges duplicates, rewrites unclear ones, and deletes stale ones. Every launch
receives a pointer to a bounded rendered file and reads it when useful. Humans
can see, edit, and delete every lesson, and a human-edited lesson is not
rewritten by an agent.

## Why not the graph

The graph holds research meaning: questions, claims, evidence, decisions.
"Submit with `--gres=gpu:1`" is not a research fact, and putting it in the
append-only Patch log would mix operational know-how into history that branch
merge, replay, and the paper read as research. Glossary was the nearest
precedent, but a glossary term is still part of the research vocabulary.

## Why not the repository's agent instruction files

Providers already read `AGENTS.md` or `CLAUDE.md`, but those files belong to
the human's repository. Artifacts moved out of the repository for the same
reason ([decision](2026-09-29-artifacts-are-rcp-owned-and-edited-in-place.md)).

## Why record during the day and consolidate at night

The agent that hit the problem knows the lesson; a later turn cannot read its
transcript (the native chat context rule). So lessons are written on the hot path, and the
nightly turn does the slow work of merging and pruning, the same split as
encoding during the day and consolidating while asleep.

## Why a pointer, not inline prompt text

Inline text costs tokens on every launch and grows with the store. A pointer
costs nothing until read. The rendered file has a size cap in `limits.py`,
which forces consolidation to prune rather than accumulate.

## Why a lesson is never authority

A lesson is context, like mail: it cannot widen capability, write roots, graph
target, or budget, and it cannot assert a research claim. An agent that follows
a wrong lesson has made an ordinary mistake, which the human can fix by editing
or deleting the lesson.
