---
id: research-graph-audit
kind: workflow
label: Research graph audit
version: 1.0.0
description: Review graph structure and evidence provenance in a deliberate sequence.
dependencies:
- graph-audit@1.0.0
- evidence-triage@1.0.0
---

# Research graph audit

1. Read the graph and rendered research pointers supplied by RCP.
2. Use Graph audit to identify structural gaps without changing canonical state.
3. Use Evidence triage to check whether the important claims have traceable support.
4. Return a compact report with observations, uncertainty, and human-controlled next actions.

This is a prompt-level workflow. RCP stages this file and its declared skills; it does not execute
the numbered steps itself.
