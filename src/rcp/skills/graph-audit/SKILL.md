---
id: graph-audit
kind: skill
label: Graph audit
version: 2.0.0
description: Review the project graph for claims that outrun their support, relations that hide their reasoning, duplicate identities, and summaries that overstate standing.
dependencies:
---

# Graph audit

A research graph decays quietly. Nothing breaks; it just gradually stops meaning what it says. A
hypothesis keeps a status set three revisions ago after the evidence moved. Two nodes describe the
same experiment under different names, and each accumulates half the relations. A summary sentence
written when a result was preliminary still reads as settled. None of this shows up as an error — it
shows up months later as a reader trusting a claim that was never established.

An audit is a read pass whose output is a report for a human. You are not repairing the graph, and
you are not proposing edits unless the task asked for them.

## Read in this order

1. **`research.md`** first, as a reader would. Note every sentence you would believe on first
   reading. Those are the claims the graph is actually making.
2. **`graph.json`** second, to check whether each of those claims is carried by nodes, relations,
   standing, and evidence that support it.

Reading the rendering first is deliberate. If you start in the JSON you will audit the data
structure, which is almost always fine, and miss the thing that matters: what the project appears to
assert to someone who reads the prose.

## What to look for

**Claims that outrun their support.** A hypothesis at `supported` whose only inbound evidence is
preliminary, qualified, or attached to a different question. A conclusion in the rendering that no
evidence node establishes. A result stated without the caveat its own interpretation records.

**Relations that hide their reasoning.** An edge exists but nothing explains why this evidence bears
on that hypothesis. `supports` where the evidence measures the apparatus rather than the claim. An
experiment connected to a hypothesis it cannot discriminate between.

**Split or duplicate identity.** Two nodes for one thing, each holding part of the relations, so
neither reads as complete. This is the most common structural defect and the hardest to see from
inside a single node — it becomes visible only when you list the titles of one type together and
read them as a set.

**Orphans that should not be orphans.** Evidence attached to no experiment. An experiment attached to
no hypothesis or decision. A blocker nothing is blocked by. Some isolation is legitimate — a newly
recorded observation waiting to be placed — so ask whether the missing link is an omission or an
honest "not yet connected".

**Stale status.** A decision still `open` whose question the graph has since answered. A blocker
still `open` whose resolution condition the evidence shows was met. An experiment in `running` whose
evidence describes a completed analysis.

**Ambiguities that stopped being open questions.** An ambiguity whose question a later node quietly
answered, or whose related nodes have moved on. These are cheap to close, and they crowd the human's
attention queue while they sit.

## Report format

Structure the report so a human can act on it without re-deriving your reasoning:

```
## Observations
What the graph currently says, with node ids, and what in the graph does or does not support it.

## Concerns
Each concern: what reads wrong, which nodes are involved, and what a reader would wrongly conclude.
Ordered by how badly a reader would be misled, not by how easy the fix is.

## Suggested human actions
Concrete and human-owned: a status a person could change, a merge a person could approve, an
ambiguity a person could answer. Say what each would fix.

## Checked and sound
Briefly — what you verified that was fine. This is what makes the report trustworthy rather than a
list of complaints.
```

Keep observation separate from recommendation throughout. "This hypothesis is `supported` while both
inbound evidence nodes are qualified" is an observation. "Move it back to `proposed`" is a
recommendation, and it is the human's call.

## What an audit does not do

Do not edit canonical `.research` files. Do not change standing, status, or truth membership — those
are human authority, and an audit that quietly fixed things would destroy the record it was asked to
check.

Do not invent causal explanations for what you find. "These two nodes describe the same run" is
supportable. "They diverged because the second session lost context" is a story about history you
cannot read.

If the audit surfaces a genuine gap in the project's vocabulary, or a boundary nobody has set, that
is an Ambiguity for the human, not a finding you resolve.
