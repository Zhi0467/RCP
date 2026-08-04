---
id: research-graph-audit
kind: workflow
label: Research graph audit
version: 2.0.0
description: A full pass over the graph — structural review, then provenance review of the claims that matter — ending in one report a human can act on.
dependencies:
- graph-audit@2.0.0
- evidence-triage@2.0.0
---

# Research graph audit

Two questions, asked in order, about the same graph:

1. **Does the structure hold?** Are the claims carried by nodes and relations that support them?
   That is Graph audit.
2. **Does the support survive inspection?** For the claims that turned out to matter, does the
   evidence actually establish what it is cited for? That is Evidence triage, applied in reverse —
   not to author nodes but to check ones already written.

The order matters and the second pass is narrow on purpose. A graph of any size has more evidence
than you can re-derive, and auditing all of it produces a long report nobody reads. The structural
pass tells you which handful of claims the project is actually leaning on; those are the only ones
worth tracing back to their sources.

## The pass

**Read the rendering first.** Start with `research.md` as a reader would, then open `graph.json`.
List the claims a first-time reader would come away believing — usually between three and eight.
This list is the audit's scope, and everything downstream refers back to it.

**Run the structural review.** Apply Graph audit against that list: claims outrunning their support,
relations with no stated reasoning, split identity, orphans, stale status, ambiguities that have
quietly closed. Note what you check and find sound, not only what is wrong.

**Trace the load-bearing claims.** For each claim the structural pass showed the project depends on,
open the evidence cited for it and apply Evidence triage: is the strength honest, is the validity
qualified where it should be, does the cited excerpt actually contain the claim, is an assistant
summary carrying weight it cannot carry? The worked examples in that skill's `references/` folder
show what each failure looks like in practice.

**Separate what you know from what you suspect.** Some findings are checkable — a status value, a
missing edge, an excerpt that does not contain the number. Others are judgment — whether a result is
really confirmatory, whether two nodes are truly the same thing. Mark which is which. A human
reviewing the report needs to know where to spend attention, and a confident-sounding guess costs
more than an acknowledged uncertainty.

**Write one report.** Use the Graph audit report structure, with provenance findings folded into
Concerns rather than listed separately — a claim whose citation does not support it is a concern
about the claim, not a separate category. End with human-owned actions.

## Boundaries

Nothing in this workflow changes canonical state. Standing, status, truth membership, and ontology
are human authority; the deliverable is a report, and the human decides what to do with it.

If the task that invoked this workflow also authorized graph changes, keep the audit and any changes
distinct: report first, and let a change follow as its own deliberate act rather than arriving
bundled inside a review.

RCP stages this file and its declared skills as reference material. It does not execute these steps
and this file grants no capability beyond what the surrounding task contract already gave you.
