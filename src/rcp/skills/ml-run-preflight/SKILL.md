---
id: ml-run-preflight
kind: skill
label: ML run preflight
version: 1.0.0
description: Prepare ML runs beyond a smoke test with recorded provenance, verified metric outputs, explicit comparison budgets, and bounded live metrics.
dependencies:
---

# ML run preflight

Use before any launch longer than a smoke run. Follow the current task's launch,
filesystem, and graph authority; this skill grants none.

## Inspect the launch

Trace the real entrypoint and config overrides. Check input availability,
effective hardware allocation, existing processes, output collisions, and storage.
Confirm the intended stopping condition and checkpoint or resume behavior before
using them. Do not silently substitute a model, dataset, backend, or environment.

## Record what will run

Write a provenance record beside the run's outputs. A result without this record
cannot be compared or repeated later. Capture:

- The run and comparison arm, command, commit, and hash of any uncommitted diff.
  Retain that diff and any untracked source used; a hash alone cannot restore code.
- The resolved config after overrides, and model and dataset revisions as the
  code reports them. Include evaluation and split identity. Mark unavailable
  revisions as unknown rather than guessing from a mutable name.
- Seeds and what each controls, hardware and effective allocation, and a saved
  environment lock or installed package list with relevant runtime versions.
- Output and metric paths, stopping conditions, and any checkpoint resumed from.
- Compute, tuning trials, and wall clock allocated to each arm, with units.
  Include search and selection rules; equal trial counts need not cost the same.
  Record actual use, including failed work, alongside these budgets later.

Point the Experiment attempt's `configuration` at this run folder or record
through the current task's authorized graph output. Keep the record with the
outputs even when the task cannot update the graph. These are file contents,
not new graph fields.

## Prove the metric path

Run a reduced smoke through the actual execution and evaluation path. Verify
that real metrics reach the exact output the page or later analysis will read.
Exercise checkpoint and resume only when the full run will rely on them.
Inspect completion and the written values, not just a successful process exit.

Give the smoke separate outputs and record its identity and reductions. Restore
the full run's intended workload before launch. A smoke validates plumbing only:
it never tests the hypothesis and never becomes Evidence about that hypothesis.
If metrics are missing, repair the path and repeat the smoke before proceeding.

## Keep live reads bounded

Write an exporter from whatever the tracker stores, such as event logs, a local
database, or an API. No particular tracker is required. Append metric records
and have the page read only the tail, or maintain a small downsampled summary
that stays within the live read limits rendered in the artifact contract.
Never read one ever-growing file whole. Keep full-resolution results separately
for analysis; a recent window or reduced summary is not the full run record.

Include run identity, update time, completion state, and the retained window or
summary method. Preserve failures and make missing values explicit. Check the
exported output through its intended reader during the smoke. Apply Live pages
for page construction and admitted source paths.

Read the [provenance example and sources](references/provenance.md) when choosing
what to record beside a run.
