---
id: episode-report
kind: skill
label: Episode report
version: 1.1.0
description: Create the required durable visual HTML wrap-up for an RCP episode, explaining its work, evidence, limits, ending, and next human decision without changing project or graph state.
dependencies:
---

# Episode report

Produce one self-contained, valid HTML report at the exact output path in the
episode wrap-up instruction. This is a retrospective for a researcher taking
over from the episode, not another operational research turn.

Start with the research outcome in plain language: what question was pursued,
what changed, what evidence supports that change, and what the human needs to
decide next. A reader should understand that opening without knowing the task
ids, internal protocol, or preceding conversation. Use concrete node titles and
research terms; keep identifiers and execution details in supporting provenance.
Explain an unfamiliar term when it first matters. Prefer a few short paragraphs
and direct labels over a dense chronology or a wall of status tables.

Make the report inherently visual. Use an intentional visual hierarchy plus the
charts, diagrams, timelines, matrices, annotated evidence maps, or other visual
forms that best expose the episode's structure and conclusions. Do not merely
decorate a prose memo. Keep every visual honest about missing evidence and
uncertainty. The HTML must remain useful in RCP's opaque sandbox without network
resources.

State why the episode ended and distinguish observations from interpretation.
If it exhausted its operational ceiling, failed, or paused for human authority,
make the partial boundary conspicuous and never imply unfinished work happened.
Use only the compact immutable episode receipt supplied for this continuation
and the native session's existing context. Do not seek or rebuild graph,
research, transcript, or repository context during wrap-up.

## Experiment-loop guide

For an `experiment_loop` episode, emphasize the objective, method and relevant
configuration, scientifically meaningful attempts, observations, evidence,
failure analysis, limitations, and the exact completion or human-authority pause.
End with the next falsifying test or human decision that the evidence supports.

## Auto-research guide

For an `auto_research` episode, also explain epistemic movement across the
research graph, Decisions made or awaiting authority, delegated-agent and worker
orchestration, what progressed or failed, unresolved uncertainty, and a concise
briefing that lets the researcher resume control without reconstructing the
episode chronology.

Show the meaningful before/after research changes when the supplied context
supports them, with evidence and unresolved questions beside each conclusion.
Distinguish results recorded on the episode branch from changes already merged
to main. Use a small task/dependency diagram only when known relationships help
explain the result; do not invent an execution trace from a list of task ids.

## Authority boundary

The report is descriptive only. It has no Patch, watcher, command, Proposal, or
graph-authority output. Do not write or modify any file except the exact report
output. The current graph and Patch history remain the sources of project truth.
