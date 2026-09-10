---
id: experiment-causality
kind: skill
label: Experiment causality
version: 1.2.0
description: Plan, repair, or audit experiment dependencies by tracing input gates and separating intended empirical handoffs from observed Evidence.
dependencies:
---

# Experiment causality

Trace every main or next Experiment back through the conditions that make it runnable. Distinguish
what is planned, what has been observed, and what still needs a choice or an external change.

## Set authority first

Use the authority and graph boundary in the current task contract. In a read-only audit, including
Research graph audit, report findings only. When construction or repair is authorized, contribute
only to that task's Patch. Decision choices, standing, and lifecycle updates follow that contract;
this skill grants none. Changes to existing ResearchQuestions or Hypotheses always use a Proposal,
even when new Evidence motivates the change. No agent may approve a Proposal.

## Trace each main Experiment

1. **List its gates.** Collect Decisions reached through `governed_by`, Blockers reached through
   `blocked_by`, and prerequisites stated only in prose.
2. **Classify how each gate is settled:** a choice, external availability, ordinary operational
   work, existing Evidence, or a new empirical result.
3. **Plan empirical work without inventing its result.** Create or reuse the smallest bounded
   precursor Experiment when a new observation is needed. Name the downstream gate and the
   intended observation in its design or current summary. Connect the main Experiment to its
   actual gate now. Keep the gate unresolved and create no Evidence for an unrun Experiment.
4. **Record the observed handoff.** When that precursor yields a result, inspect it, record its
   Evidence, and connect:

   ```text
   precursor Experiment -> produces -> Evidence
   Evidence -> informs -> downstream Decision
   Evidence -> addresses -> downstream Blocker
   main Experiment -> governed_by or blocked_by -> downstream gate
   ```

   Use only the applicable Evidence-to-gate edge. `informs` does not choose a Decision; `addresses`
   does not change Blocker status.
5. **Recurse through the precursor.** Identify its genuine input Decisions and Blockers, classify
   their resolution sources, and repeat only for empirical gates. Never move a gate that the
   precursor will settle backward into that precursor's inputs.
6. **Stop at a real resolution source.** Do not invent Experiments for choices, external
   outages, repository access, implementation tasks, ordinary retries, or already sufficient
   Evidence.

## Smoke and validation Experiments

An Experiment whose objective is to verify infrastructure, integration, or recovery is itself
the resolution path for that uncertainty. Its unpinned parameters, unbuilt images, and unrun checks
are steps of its own work. Write them into `design`, `expected_outcomes`, and
`interpretation_rules` so the episode can start and perform them. An open Blocker reached through
`blocked_by` keeps RCP from starting the Experiment, so the smoke itself carries `blocked_by` only
for a constraint the run cannot remove, such as a credential nobody has granted or a hardware
allocation, with a `resolution_condition` that does not require running the smoke.

The unverified infrastructure remains a genuine gate for any downstream main Experiment that
depends on it. Keep that Blocker, connect the main Experiment to it with `blocked_by`, name the
smoke as the precursor in the Blocker's `resolution_condition`, and connect the smoke's Evidence to
it with `addresses` once the smoke has run. The gate moves off the precursor, not out of the graph.

## Check the complete action program

- **Reversed:** a downstream Decision governs, or downstream Blocker blocks, the precursor meant to
  inform or address it.
- **Prose-only:** a genuine input gate or observed Evidence handoff exists only in summaries.
  A planned precursor names its intended handoff in prose until Evidence exists.
- **Circular:** following gates and empirical resolution paths returns to the same node.
- **Self-blocking:** an Experiment is blocked by the condition its own Evidence is meant to
  address, or by a Blocker whose `resolution_condition` amounts to running that Experiment or to
  setup the Experiment performs itself.
- **Stale:** lifecycle text or status conflicts with later Evidence or action edges.
- **Duplicate:** parallel nodes or paths represent the same gate, Experiment, or Evidence.
- **Incomplete:** a gate requiring a new measurement has no planned precursor, or a known
  precursor's result lacks its producing Experiment or downstream handoff. Pending measurement is
  unresolved work, not missing Evidence to fill in.

Reuse existing node identities and relations whenever they express the same entity. Do not create a
second path merely to make the chain visually complete.

## Finish

Before reporting or writing a Patch, rerun the trace. For each main Experiment, state its gates,
their resolution sources, and either the observed path or the intended next step. A coherent plan
can still have unresolved measurement, choice, or external gates.

See [planning and result examples](references/worked-examples.md) for complete Patches and the
different Decision actions available to ordinary work and an authorized orchestrator.
