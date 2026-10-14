---
id: ml-result-rigor
kind: skill
label: ML result rigor
version: 1.0.0
description: Check ML results for misleading comparisons and record the runs, uncertainty, budgets, and limits needed to use them as Evidence.
dependencies:
---

# ML result rigor

Review apparent wins and losses alike before relying on a result. Follow the
current task's authority and output contract; this skill grants no permission.

## Check the traps

For each trap, record `clear`, `concern`, `unknown`, or `not applicable`, with an
artifact pointer and reason. Use `clear` only within the check's tested scope;
missing information is `unknown`, not a failed check. Explain non-applicability.
Carry unresolved concerns and unknowns into the result's qualifications.

**Unequal tuning effort.**
Check each arm's search space, selection rule, trials, and total tuning compute.
If unavailable or unequal, state that the method's gain cannot be separated from tuning effort.

**Unequal execution budget.**
Compare allocated and consumed compute, wall clock, hardware, and relevant inference limits.
If not comparable, describe the resource difference; if unrecorded, say efficiency is unresolved.

**Too few independent runs.**
Count completed independent runs per arm and inspect effect size against run-to-run spread.
If replication is absent or precision inadequate, state that stability across runs is unestablished.

**Repeated measurements counted as independent runs.**
Identify independent units and variation from initialization, data sampling, splits, and tuning.
If dependencies cannot be assessed, name them; checkpoints or repeated samples do not establish retraining variance.

**Training or selection leakage.**
Audit split boundaries, duplicates, preprocessing fits, and test access during selection.
If access history is missing, say held-out evaluation is unverified; qualify any known leakage.

**Benchmark contamination.**
Check model/data revisions, exposure dates, overlap, and retrieved answer sources.
If exposure cannot be traced, state that uncertainty; a negative detection check is not proof of a clean benchmark.

**Different evaluation conditions.**
Match evaluation set/version, subsets, scoring code, prompts, decoding, judges, and aggregation as applicable.
If arms cannot be reevaluated under one protocol, name the differences and limit the comparison.

**Weak or stale baseline.**
Reproduce a relevant baseline in the current setup and inspect its implementation and tuning effort.
If that cannot be done, label the baseline unverified and avoid claiming an advance over a well-tuned alternative.

**Incomplete runs or outputs.**
Check completion, expected versus processed items or steps, and output truncation or stop reasons.
If completeness is unresolved, label a partial snapshot; keep smoke, crash, timeout, and completed results distinct.

**Selected successes hiding failures.**
Reconcile every attempted run with reported results, exclusions, and task or example selection.
If the ledger is incomplete, say selection bias is unresolved; retain failures and unfavorable subsets.

**Adaptive search presented as confirmation.**
Trace tested candidates, metrics, and keep/discard decisions; seek evaluation unused by selection.
Without independent confirmation or justified statistical treatment of repeated testing, label the result exploratory.

**Misleading spread or averages.**
Check task weights, per-task values, and whether spread means standard deviation, standard error, or a confidence interval; name its computation and independent unit.
If raw values or the method are missing, mark uncertainty unverified rather than inventing an interval.

**A score gain mistaken for a mechanism.**
Use controls that isolate the changed component before attributing the effect to it.
Without those controls, report the measured difference and label the mechanism an untested explanation.

**Claims beyond tested scope or useful effect.**
Compare the effect's size and uncertainty with the claimed population, conditions, and practical use.
If those uses were not tested, restrict scope; an inconclusive difference does not establish equivalence.

## Make a comparison usable as Evidence

Keep a comparison record with primary result and provenance pointers. Name each
arm, planned/completed/failed runs, raw per-run values, seeds, and what varied
between independent runs. State the difference and spread, how each was computed,
and what uncertainty they cover. Do not infer retraining stability from repeated
evaluation of one fixed model. Use paired differences only for a paired design.

Name the shared evaluation set and version, metric and direction, tuning budget
each arm received, actual resource use, exclusions and reasons, and the tested
scope. Preserve failures even when no score exists. Report missing information.

Apply Evidence triage for field choice and claim-relative assessments. A
single-run or unequal-budget comparison can still be Evidence; its edge
assessment and qualifications must reflect those limits. Search-time keep/discard
on a validation score generates candidates; it does not confirm them.

Read [worked examples and sources](references/worked-examples.md) for failed runs,
unequal tuning, and adaptive search.
