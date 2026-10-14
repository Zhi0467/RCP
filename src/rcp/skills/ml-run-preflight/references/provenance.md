# Provenance beside the outputs

This illustrative record belongs in `runs/comparison/arm-b/run-17/provenance.md`.
Names and values are examples, not required fields or recommended budgets.
Replace revision placeholders with values captured from the executing code.

```text
Run: arm-b/run-17; comparison: baseline versus revised solver
Command: uv run python evaluate.py --config resolved.json
Code: commit <full commit>; dirty.patch SHA-256 <digest>
Untracked source: none; dirty.patch retained beside this record
Resolved config: resolved.json, emitted after command-line overrides
Model: checkpoint <reported revision>; tokenizer: not applicable
Dataset: task-suite <reported revision>; split: held-out-v3 <content digest>
Evaluator: score.py at the recorded code revision; metric: mean error, lower is better
Seeds: solver=17, item-order=41; fixed held-out items across arms
Hardware: one allocated accelerator <model>, <memory>; eight allocated CPU cores
Environment: environment.lock; installed-packages.txt; runtime/driver <versions>
Outputs: scores.jsonl; logs/; no checkpoint resume
Stop: evaluate every item in the recorded split or reach the declared wall limit
Budgets per arm: 6 tuning trials, 12 accelerator-hours, 18 elapsed hours
Selection: lowest validation error within the same declared search ranges
Actual consumption: pending; append tuning, failures, and evaluation usage at completion
Smoke: smoke/run-17; reduced item list; same scorer and exporter; completed
Smoke check: scores written and summary read successfully; plumbing only
Full config check: reduced smoke item limit removed
Live output: summary.json, downsampled by progress interval with extrema retained
Live metadata: run id, update time, counts, status, summary method, raw result path
```

The attempt's `configuration` points to this record or its run folder. This is
an ordinary file record, not a proposed graph schema. Capture source snapshots
for untracked code when present. A diff hash identifies bytes but cannot recover
missing source. Keep the smoke separate from the full result and retain raw
results when replacing the compact live summary.

# Sources

Original method prose; no source text or code is incorporated.

- [NeurIPS Paper Checklist](https://nips.cc/public/guides/PaperChecklist):
  reproduction details, uncertainty sources, and total compute including failed work.
- Dodge et al. (2019), [Show Your Work: Improved Reporting of Experimental Results](https://aclanthology.org/D19-1224/):
  tuning and selection budgets as part of a comparison.
- Bouthillier et al. (2021), [Accounting for Variance in Machine Learning Benchmarks](https://arxiv.org/abs/2103.03098):
  record variation sources beyond initialization.
- [NVIDIA Skills, inspected revision](https://github.com/NVIDIA/skills/tree/009a566744dee8161fbe1365d8c9d093e6654da7):
  execution-path preflight and the distinction between smoke checks and hypothesis tests.
- [Hugging Face Skills, inspected revision](https://github.com/huggingface/skills/tree/c3ff942be3e07116b475253f0a8fc4f54682c1a8):
  reduced evaluation and persistence checks before larger runs.
