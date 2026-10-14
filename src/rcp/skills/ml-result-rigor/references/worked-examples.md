# Worked examples

These are illustrative result records, not minimum run counts, budget rules,
or new graph fields. Apply Evidence triage when representing them in the graph.

## A comparison with one failed run

Arms A and B use the same held-out task set `suite-v3`, content digest recorded,
and the same evaluator revision. Higher scores are better. Each arm planned
three independently initialized runs with seeds 11, 22, and 33; data, evaluation
items, and tuning choices stayed fixed. A completed with scores 70, 72, 74.
B completed with 73 and 75; its seed-33 run exhausted memory before evaluation.
The ledger retains that failure and its logs, with no invented score.

Each arm received four tuning trials and the same declared compute and wall
limits; the attached ledger includes actual use and failed work. A's mean is 72,
with sample standard deviation 2. B's completed-run mean is 74, with sample
standard deviation about 1.41. Both deviations use the sample formula with
completed-run count minus one in the denominator; neither is a confidence interval.
The difference of completed-run means is +2, not an estimate of failed-run quality.

Completion is a `concern`: B's failure is excluded from the score summary because
no evaluation exists, and the summary may favor surviving runs. The assessment
qualifies the difference as conditional on completion, within this suite and
allocation. It does not establish reliable superiority or variability over new
training datasets. Primary pointers include every score, config, and failure log.

## Unequal tuning budgets

A used a fixed published configuration. B selected from twelve trials using
validation error. Both final models were evaluated once on the same untouched
`holdout-v2` with the same scoring code. The run ledger records search ranges,
selection rule, compute, elapsed time, and final raw scores for both arms.

Tuning fairness is a `concern`; independent-run stability is `unknown` because
neither method was repeated. The comparison can still become Evidence about
these selected models under their recorded budgets. Qualify any edge about the
method itself: the result does not separate the change from extra tuning, and
run-to-run spread is unavailable. Do not invent it from variation across test items.

## An adaptive search result

An optimizer tried successive candidates and kept whichever improved a validation
score. Its ledger preserves discarded candidates, failures, metric changes, and
total search cost. The final candidate has the best observed validation score.

Confirmation is a `concern`: selection repeatedly used this score. Record the
improvement as an exploratory observation about the search, not confirmation of
the selected candidate's general advantage. A reserved evaluation unused in
selection could test that claim. Until then, say independent confirmation is
missing. If benchmark overlap checks found no match, report the checked sources
and method; unknown training or retrieval exposure remains unknown.

# Sources

Original synthesis and examples; no source text or code is incorporated.

- [NeurIPS Paper Checklist](https://nips.cc/public/guides/PaperChecklist):
  complete reporting, uncertainty definitions, compute, and claim scope.
- Dodge et al. (2019), [Show Your Work: Improved Reporting of Experimental Results](https://aclanthology.org/D19-1224/):
  search budgets, selection, and comparison fairness.
- Agarwal et al. (2021), [Deep Reinforcement Learning at the Edge of the Statistical Precipice](https://arxiv.org/abs/2108.13264):
  few-run uncertainty and aggregation; no universal replication count is inferred.
- Bouthillier et al. (2021), [Accounting for Variance in Machine Learning Benchmarks](https://arxiv.org/abs/2103.03098):
  independent variation sources and limits of seed-only replication.
- Henderson et al. (2018), [Deep Reinforcement Learning That Matters](https://ojs.aaai.org/index.php/AAAI/article/view/11694):
  implementation, baseline, evaluation, and seed sensitivity.
- Lipton and Steinhardt (2018), [Troubling Trends in Machine Learning Scholarship](https://arxiv.org/abs/1807.03341):
  separating measured gains, explanations, and speculation.
- Fu et al. (2025), [Does Data Contamination Detection Work (Well)?](https://aclanthology.org/2025.findings-naacl.291/):
  detector assumptions and limits; a negative check cannot certify no exposure.
- Chen et al. (2025), [From Static to Dynamic Evaluation](https://aclanthology.org/2025.emnlp-main.511/):
  changing evaluations still need reproducible, comparable conditions.
- Wang et al. (2026), [Search-Time Contamination](https://arxiv.org/html/2606.05241v1):
  retrieval can expose benchmark answers; this preprint's setting does not establish a universal effect size.
- [K-Dense Scientific Agent Skills, inspected revision](https://github.com/K-Dense-AI/scientific-agent-skills/tree/a1043ef544e3a100c28e70a736b6cf32554d0677):
  independent units, leakage, and connecting each criticism to evidence and limits.
- [Orchestra AI-research skills, inspected revision](https://github.com/Orchestra-Research/AI-research-SKILLs/tree/773a52944ba4747a18bd4ae9ade53fff041adcbc):
  preserving negative results and separating exploration from confirmation.
