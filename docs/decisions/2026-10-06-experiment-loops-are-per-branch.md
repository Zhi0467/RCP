# Experiment loops are per graph branch

Confirmed by the human 2026-10-06.

## Decision

An Experiment loop belongs to its project, node, and graph target. Each target
holds at most one live loop per node. An Auto-research orchestrator acts only on
its own branch. It reads other branches and asks the human about them, and it
cannot stop, replace, or adopt a loop it did not start.

Overlap between targets is shown, not prevented: starts succeed, and every loop
agent is told which loops run elsewhere on its node and asked to check with the
human when its work could interfere.

## Why

The earlier rule allowed one live loop per node across the whole project, so a
branch could not duplicate real-world work. It was enforced by a project-wide
index and by an orchestrator kickoff that stopped the existing loop as a
"predecessor". On 2026-10-06 that path stopped a human's main loop from a
branch, silently, and left the human's chat and watchers out of step. Graph
branches are meant to isolate work. A branch that can stop main breaks that
isolation and the human-authority boundary
([invariant 3](../../AGENTS.md)).

Information instead of a gate keeps both branches usable. Duplicate compute is
a coordination problem, which the human and the agents resolve once each can see
the other. A gate would block legitimate variants, and it cannot read the human's
intent from a free-text answer ([invariant 3b](../../AGENTS.md)).

## Not chosen

- Approve-then-RCP-acts requests from the orchestrator: a new authority path for
  an action the human can take directly.
- Requiring a code worktree for branch loops or overlapping starts: deferred.
  Code isolation stays optional; visibility comes first.
