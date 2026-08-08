# Worked examples

Use these generic examples to calibrate the narrow claim carried by Evidence.

## Calibration Evidence informs a Decision

> **observation** Three completed calibration runs show stable throughput through batch 16, then
> memory failures at batch 24.
> **interpretation** This bounds the feasible batch-size options for the main run. It does not
> choose among the feasible options.
> **strength** confirmatory  **validity** valid  **origin** internal_run
> **relations** calibration Experiment `produces` this Evidence; this Evidence `informs` the batch
> size Decision.

The measurements establish the feasible set. A human-owned action records which option is selected.
Do not make the Decision govern the calibration Experiment when the calibration exists to inform it.

## Smoke Evidence addresses a Blocker

> **observation** The smoke run completed one end-to-end batch, wrote the expected artifact, and
> passed the schema check.
> **interpretation** This addresses the unverified-pipeline Blocker for the main run. The short run
> does not test the scientific effect targeted by the main Experiment.
> **strength** confirmatory  **validity** valid  **origin** internal_run
> **relations** smoke Experiment `produces` this Evidence; this Evidence `addresses` the pipeline
> Blocker.

The Evidence can justify a later lifecycle update, but the `addresses` edge does not itself close
the Blocker. Do not attach the downstream Blocker as an input that blocks its own smoke test.

## An incomplete run is a snapshot

> **observation** At the cited timestamp, the job was healthy at step 117 of 120 with no recorded
> traceback or resource failure.
> **interpretation** This is preliminary runtime evidence only; completion and final evaluation
> remain unobserved.
> **strength** preliminary  **validity** qualified  **origin** internal_run

Put the time boundary in the observation. Later readers must not mistake a monitoring snapshot for
live state or a completed result.

## A citation must carry its claim

> **observation** Peak memory remained below the configured limit.
> **source excerpt** “The launch uses the approved runtime configuration.”

The excerpt does not contain a memory measurement. Cite the metric artifact or the exact record that
reports the peak. A source from the right conversation is not sufficient provenance.

## Suggestions are not Evidence

> **record** “We should use the smaller checkpoint interval.”

Record this as a proposed Decision or an Ambiguity when it matters. It is neither an empirical
observation nor proof that the option was selected.
