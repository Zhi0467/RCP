---
id: campaign-report
kind: skill
label: Campaign report
version: 1.0.0
description: Create the required durable HTML wrap-up for an RCP auto-research campaign at any ending, making its reasoning, decisions, failures, progress, and remaining human work legible without changing graph state.
dependencies:
---

# Campaign report

Produce one valid HTML report for RCP to capture durably and render through its existing sandboxed
HTML artifact boundary.

Make the campaign's reasoning and decisions legible, together with what failed, what progressed,
and what still awaits a human. If the campaign ended through exhaustion, Stop, or failure, make the
report clearly partial and do not imply that unfinished work happened or succeeded.

Choose the form that best communicates this campaign. Include useful visualizations or artifacts
when they make the account clearer. The report is retrospective: it carries no graph authority and
does not replace `patch.json` or the current graph as the source of graph facts.
