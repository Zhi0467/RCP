---
id: counterfactual-research
kind: skill
label: Counterfactual research
version: 1.0.0
description: Re-check or re-run an earlier step of the research graph, in place or on a fork from a past revision, and read what changed.
dependencies:
---

# Counterfactual research

Use this when a later result conflicts with an earlier assessment, when progress has stalled, or
when an important belief rests on one Evidence node or one Decision choice. It asks: would the
research still reach this belief if that step had gone differently?

## Set authority first

Use the authority, graph target, and budget in the current task contract; this skill grants none.
If your contract does not list a verb named below, recommend the move in your answer instead of
attempting it. Protected beliefs keep their Proposal rules everywhere, including on a fork.

## Choose the lightest move

1. **Re-check in place.** Re-examine revision R on the current branch. Read it with `graph_log`
   and `graph_diff`, check its sources and reasoning, and record any correction as an ordinary
   change. Nothing is hidden from you, so this suits verification, not independence.
2. **Reason in place.** Ask what the graph would support without that Evidence or with the other
   Decision option, and answer in prose. Nothing is written. This is cheap and good for triage.
3. **Fork.** Put agents on a branch that starts just before revision R when the alternative needs
   real work (new Experiments, a re-run, another Decision option) and must not be shaped by what
   came after R. Agents on a fork cannot read the parent's later revisions.

## Write a good fork

- **Fork point.** Use `graph_log` to find the revision you doubt, then fork from the revision just
  before it: `spawn --at <ref>@<R-1>` with a node seat or `--project`.
- **Seed.** Express the counterfactual as the smallest setup Patch (`--seed-file`): remove or
  replace one Evidence node, or choose another Decision option. Change one thing.
- **Instruction.** State the question the fork answers and what ends the job. Do not mention
  conclusions reached after the fork point; that would undo the fork's independence.
- **Seats.** Seat a worker on a node when its job belongs to that node. Use a project seat when it
  does not, for example an independent audit across a search.
- **Budget.** Fork workers and Experiments spend your budget like any child.

## Read the result

1. Compare claims, not node ids: `graph_diff <your branch>@head <fork>@head`. Equivalent claims may
   carry different ids.
2. Weigh it honestly. One agreeing fork is weak support. A divergence is a lead: find the Evidence
   or Decision that drives it before changing standing.
3. Then do one of three things. Merge the fork back into its owned source when its work should be
   kept (`merge`). Apply a targeted change on your own branch. Or archive the fork
   (`branch --archive`); its history is kept.

## Limits

A fork hides later graph revisions only. Repositories, files, operational lessons, and provider
memory can still carry later information. Say so when you report a fork's result.
