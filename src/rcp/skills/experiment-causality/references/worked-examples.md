# Planning, results, and a choice

These illustrative Patches form one sequence in an initially empty graph with repository alias
`repo`. The first two fit ordinary graph-capable Work. The third requires an outer task that
explicitly gives the orchestrator Decision authority. Use the live schema and task boundary for
real work; the example observations and artifact path are fictional.

## 1. Plan the precursor and the downstream gate

The main comparison needs a measurement duration. A short calibration will measure repeatability
at two durations. No calibration has run, so the Patch records an open Decision and two proposed
Experiments. The precursor's design names its intended handoff; there is no future Evidence node.
The Decision governs the main comparison, not the calibration that will inform it.

```json
{
  "summary": "Plan a calibration before choosing the main comparison's measurement duration.",
  "ops": [
    {
      "op": "create_nodes",
      "nodes": [
        {
          "id": "exp/calibrate-duration",
          "type": "experiment",
          "title": "Calibrate measurement duration",
          "objective": "Measure repeatability at the candidate durations.",
          "design": "Repeat three measurements at each duration; use their spread to inform dec/measurement-duration.",
          "completion_criteria": ["Record all six measurements and compare their spread."],
          "status": "proposed"
        },
        {
          "id": "dec/measurement-duration",
          "type": "decision",
          "title": "Choose measurement duration",
          "question": "Which duration balances repeatability and measurement time?",
          "options": ["5 seconds", "10 seconds"],
          "status": "open"
        },
        {
          "id": "exp/compare-methods",
          "type": "experiment",
          "title": "Compare the two methods",
          "objective": "Compare the methods using the selected measurement duration.",
          "status": "proposed"
        }
      ]
    },
    {
      "op": "create_edges",
      "edges": [
        {
          "source": "exp/compare-methods",
          "target": "dec/measurement-duration",
          "relation": "governed_by",
          "explanation": "The comparison needs a selected duration before it runs."
        }
      ]
    }
  ]
}
```

The planned precursor is not yet connected by a `produces` edge. That is an honest pending
measurement. A generic isolation advisory does not justify inventing Evidence to silence it.

## 2. Add the observed result and queue the choice

The calibration has now completed. In this example, `repo/runs/calibration/summary.json` contains
all six measurements and reports 4% spread at five seconds and 1% at ten seconds. Record that
bounded result, complete the calibration, and make the Decision ready. The main comparison remains
proposed; its completion is not a prerequisite for the choice that lets it start.

```json
{
  "summary": "Record the calibration and queue the duration choice.",
  "repositories_read": ["repo"],
  "ops": [
    {
      "op": "create_nodes",
      "nodes": [
        {
          "id": "ev/duration-calibration",
          "type": "evidence",
          "title": "Repeatability at the two measured durations",
          "observation": "Across three repetitions at each duration, relative spread was 4% at 5 seconds and 1% at 10 seconds.",
          "interpretation": "Ten-second measurements were more repeatable in this calibration and take twice as long. The main method comparison has not run.",
          "role": "result",
          "validity": "valid",
          "origin": "internal_run",
          "artifact_refs": ["repo/runs/calibration/summary.json"]
        }
      ]
    },
    {
      "op": "create_edges",
      "edges": [
        {
          "source": "exp/calibrate-duration",
          "target": "ev/duration-calibration",
          "relation": "produces",
          "explanation": "The completed calibration generated these six measurements."
        },
        {
          "source": "ev/duration-calibration",
          "target": "dec/measurement-duration",
          "relation": "informs",
          "explanation": "The observed spread makes the repeatability and time tradeoff concrete."
        }
      ]
    },
    {
      "op": "update_nodes",
      "nodes": [
        {
          "id": "exp/calibrate-duration",
          "changes": {
            "status": "completed",
            "current_summary": "All six measurements are recorded in ev/duration-calibration."
          }
        },
        {
          "id": "dec/measurement-duration",
          "changes": {"status": "ready"}
        }
      ]
    }
  ]
}
```

The Evidence does not choose the duration or support a claim about which method wins. `informs`
carries no Hypothesis assessment. In ordinary Work, report the choice to the human and proceed
only with other authorized work while it is unresolved.

## 3. Choose only when the task gives that authority

Assume an authorized Auto-research episode prioritizes repeatability over measurement time and its
orchestrator may choose this Decision. It can submit the following separate action. An ordinary
worker cannot submit it, even if the worker collected the measurements or recommends the same
option. Neither profile may approve a Proposal about an existing ResearchQuestion or Hypothesis.

```json
{
  "summary": "Choose the more repeatable duration for the main comparison.",
  "agent_action": "decision_choice",
  "ops": [
    {
      "op": "update_nodes",
      "nodes": [
        {
          "id": "dec/measurement-duration",
          "changes": {
            "selected_option": "10 seconds",
            "status": "decided",
            "rationale": "The measured 1% spread fits the episode's repeatability priority despite doubling measurement time."
          }
        }
      ]
    }
  ]
}
```

This clears this one Decision gate. The task must still admit the main Experiment's execution;
the Patch does not grant a new run, remove other gates, or change its status to completed.

## 4. Let a smoke test carry its own setup

A smoke Experiment exists to show that infrastructure works end to end before a main Experiment
depends on it. Here the model service endpoint, its credential variable, the seed prompt, and the
per-cycle budget are still unpinned, the image lacks the new lock additions, and no recovery check
has run. Those are steps the smoke performs, so they belong in its design and interpretation
rules. Blocking the smoke on them would be self-blocking: an open Blocker reached through
`blocked_by` keeps the episode from starting, and the Blocker's condition could only be met by
running the very Experiment it blocks. The main Experiment that needs the verified service keeps
the gate instead.

```json
{
  "summary": "Plan the recovery smoke with its setup folded in; gate only the main run on it.",
  "ops": [
    {
      "op": "create_nodes",
      "nodes": [
        {
          "id": "exp/recovery-smoke",
          "type": "experiment",
          "title": "Recovery smoke against the real model service",
          "objective": "Show that one reduced-budget cycle runs against the real model service and resumes across a domain boundary.",
          "design": "First pin the service endpoint, credential variable, seed prompt, and per-cycle budget and record them in the run manifest; rebuild the image with the lock additions; then run one cycle and force one resume across a domain boundary. Its Evidence addresses blk/real-service-unverified.",
          "expected_outcomes": ["One cycle completes against the real service and the resume replays the same domain state."],
          "interpretation_rules": [
            "A repairable failure while pinning or building is a setup fault to fix inside this Experiment, not a new Blocker.",
            "A constraint the run cannot clear, such as a credential or registry access nobody has granted, becomes a Blocker whose resolution condition does not name this smoke."
          ],
          "completion_criteria": ["Run manifest, cycle log, and resume log are recorded."],
          "status": "proposed"
        },
        {
          "id": "blk/real-service-unverified",
          "type": "blocker",
          "title": "Real-service recovery is unverified",
          "description": "No cycle has run against the real model service or resumed across a domain boundary.",
          "blocker_type": "infrastructure",
          "resolution_condition": "exp/recovery-smoke completes and its Evidence shows one real-service cycle and one cross-boundary resume.",
          "status": "open"
        },
        {
          "id": "exp/main-route-matrix",
          "type": "experiment",
          "title": "Main route matrix",
          "objective": "Run the full route matrix against the real model service.",
          "status": "proposed"
        }
      ]
    },
    {
      "op": "create_edges",
      "edges": [
        {
          "source": "exp/main-route-matrix",
          "target": "blk/real-service-unverified",
          "relation": "blocked_by",
          "explanation": "The full matrix should not start until the real-service recovery path is verified."
        }
      ]
    }
  ]
}
```

The smoke carries no `blocked_by` edge, so its episode can start and perform the setup. The main
Experiment keeps the gate. When the smoke completes, record its Evidence with `produces` and
`addresses`; that does not resolve the Blocker by itself.
