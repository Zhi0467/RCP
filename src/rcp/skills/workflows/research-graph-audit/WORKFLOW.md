---
id: research-graph-audit
kind: workflow
label: Research graph audit
version: 3.5.0
description: Run a deliberate read-only audit of graph structure, experiment action causality, and load bearing Evidence provenance, ending in one prioritized report.
dependencies:
- graph-audit@3.4.0
- experiment-causality@1.4.0
- evidence-triage@3.4.0
---

# Research graph audit

Produce one read-only report. This workflow and its dependencies grant no authority beyond the outer
task, and all three passes remain report-only inside this workflow.

## Pass 1: broad structure

Apply Graph audit. Read `research.md` before `graph.json` and list the claims and action state a
reader would believe. Run every Graph audit check, including broken structures, untestable claims,
and proxies read as the real quantity. Record important paths that are sound.

## Pass 2: action causality

Apply Experiment causality to every main or next Experiment, and report its defect classes. Do not
demand Evidence before measurement.

## Pass 3: narrow provenance

Apply Evidence triage only to the Evidence carrying the claims and action gates the first two
passes identified.

## Deliver one report

Use the Graph audit report structure. Fold causal and provenance findings into the same prioritized
Concerns section. Mark checkable facts separately from judgment, name the smallest next action, and
identify who has authority to take it.

This workflow writes no Patch and edits no canonical `.research` files. If the invoking task also
authorizes repairs, complete the report before that separate graph-writing step; each repair must
still fit the task's authority.
