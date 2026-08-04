---
id: evidence-triage
kind: skill
label: Evidence triage
version: 2.0.0
description: Decide what a record actually establishes before it becomes an Evidence node — provenance, strength, validity, and the boundary of the claim.
dependencies:
---

# Evidence triage

Most graph damage does not come from writing something false. It comes from writing something true
in a way that reads stronger than it is: a run that was still going recorded as a result, a check on
the measurement apparatus recorded as a finding about the science, a summary of a summary recorded
as an observation. Those nodes survive into `research.md`, into the introduction, and into decisions
about what to run next, and by then nobody can tell which sentence was load-bearing.

This skill is the pass you make between "I have read the material" and "I am writing Evidence".

## The precedence ladder

When two inputs disagree, or when you must choose what an Evidence node rests on, prefer in this
order:

1. **Primary artifacts** — the metric file, the manifest, the config, the committed checkpoint, the
   experiment output directory. These are what the run actually produced.
2. **Exact source records** — the specific conversation record, with its timestamp, where the
   number or the outcome first appears.
3. **Explicit human decisions and corrections** — a person stating a choice, a boundary, or a
   rejection. These settle project framing, never empirical fact.
4. **Reviewed synthesis** — a status document a human has read and kept current.
5. **Assistant summaries** — a model's own account of what happened.

The ladder is about *what a claim rests on*, not about what you may read. Read everything; cite
carefully.

An assistant summary may point you at evidence and may be cited alongside a primary artifact to
explain why the artifact is relevant. It cannot be the sole support for an Evidence node, because a
summary is a compression, and a compression is exactly where the qualifier gets dropped.

## Separate what happened from what it means

Evidence carries `observation` and `interpretation` as different fields, and the split is the whole
point.

`observation` is what a record or artifact states, at the granularity it states it: which job, which
step, which number, which absence. Someone who disagrees with your reading of the result should
still accept the observation as written.

`interpretation` is what that licenses for this project — and, when it matters, what it does *not*
license. An interpretation that only restates the observation in warmer words is doing no work. An
interpretation that says "this establishes the mechanism" when the run was a smoke test is doing
harm.

If you cannot write an interpretation that says something the observation does not, ask whether this
should be Evidence at all, or whether it belongs in the Experiment's `current_summary`.

## Choosing the fields honestly

**`origin`** — where the evidence came from, and it must always be set explicitly: `internal_run`
for a run or experiment in this project, `external_publication` for a paper, `external_instance` for
evidence imported from another research graph, `analytic` for a mathematical or conceptual
derivation, `unknown` only when provenance genuinely cannot be classified. `unknown` is the schema
default, so leaving it is not a choice — it is a failure to look.

**`strength`** — how much weight the result can bear:

- `diagnostic` — it tells you about the apparatus, not about the question.
- `preliminary` — real but incomplete: a run in progress, a single seed, an unrepeated measurement.
- `supporting` — a completed result consistent with a hypothesis, among others still needed.
- `confirmatory` — a completed, checked result that settles the specific claim it is attached to.

**`validity`** — whether the result stands as measured: `valid`; `qualified` (real but bounded by a
caveat you state in the interpretation); `invalid` (the measurement was wrong — keep the node,
because knowing an attempt failed changes how later results are read); `superseded` (a later
measurement replaced it).

`qualified` is the most useful and most skipped value. If your interpretation contains "but",
"only", "pending", or "still required", the validity is `qualified`.

## Boundaries you do not get to infer

**Never infer a hypothesis's scope.** Write `Hypothesis.scope` only when the exact boundary appears
in one of that hypothesis's own cited excerpts. A hypothesis about a measured effect almost always
*sounds* like it has an implied population — the model size, the domain, the number of seeds — and
writing that implied boundary in is how a project acquires limits nobody agreed to. Leave scope
empty and raise an Ambiguity asking the human for the boundary.

**Never widen a conclusion to the next question up.** A result about whether a probe measures what
it claims is not a result about the mechanism the probe was built to study.

**Never turn a suggestion into a fact.** A proposed plan, a recommended cutoff, an assistant's "we
should" — none of these are project truth until a human states the decision. If the material
contains a suggestion that matters, that is a Decision in `proposed` status or an Ambiguity, not
Evidence.

## Check the citation, not just the claim

Before you finish a node, read your own `source_refs[].excerpt` and ask: *does this excerpt contain
the claim, or does it merely come from the conversation where the claim was discussed?*

A generic excerpt — "the summary states the contract and the pending launch" — attached to a node
whose observation is a specific set of numbers means the numbers came from somewhere else. Cite
where they actually came from, or move them to `artifact_refs` and say so in the interpretation.

The tell is reuse: if the same excerpt would fit equally well under two unrelated Evidence nodes, it
is establishing neither.

## Worked examples

`references/worked-examples.md` walks through five Evidence nodes from a live continual-learning
project — what each got right, what nearly went wrong, and the triage decision behind each field.
Read it when you are unsure how strict to be; the examples calibrate `strength` and `validity`
better than the rules do.
