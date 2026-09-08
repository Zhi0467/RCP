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
