---
id: S94-decision-ripeness-and-the-agent-contract
status: implemented
tier: hermetic
driver: pytest
covered_by:
  - tests/test_agent_schema.py
  - tests/test_staged_graph_validation.py
  - tests/test_proposal_boundary.py
  - tests/test_sync.py
  - tests/test_service_contracts.py
  - tests/test_direct_ingestion_run.py
  - tests/test_api.py
  - tests/test_prompts.py
  - tests/test_experiment_loop_agent_io.py
invariants: [1, 3, 7b, 11]
reported_by: human, 2026-08-08
last_passed: 2026-08-08 — the full backend suite covered admission and replay,
  Decision choice and queue authority, Inbox predicate parity, prompt contracts,
  experiment-loop exits, and persisted Seed and Refresh answers
---

# Agents queue Decisions; only humans decide them

Confirmed by the human on 2026-08-08.

`open` means a Decision has been framed but is not yet asking for a choice.
`ready` means an agent asserts that the choice can now be made, and `revisit`
returns a previously decided question to the same queue. Ripeness is prompt
guidance, not a mechanically proved fact: agents inspect the run-scope
repositories, real experiment state, and code rather than relying only on the
graph, while RCP enforces only that a queued ballot has at least two distinct
options.

Agents may create or queue a Decision, but only the direct human Decision-choice
action may write its outcome. New Proposals target only Hypothesis status.
Ambiguities and Decision Proposals remain replayable history, but no new patch
may create or resolve one. Seed and Refresh preserve the labelled final answer
so ontology gaps and missing Hypothesis scope can be stated to the human without
manufacturing another graph object.

## Setup

Use admission patches from both agent and human authors, historical patches
containing Ambiguities and Decision Proposals, Decisions in every lifecycle
state, and Seed and Refresh tasks whose provider emits a labelled final answer.

## Assert

- `new_patches_cannot_create_or_resolve_ambiguities`
- `historical_ambiguity_patches_replay_identically`
- `new_patches_cannot_create_a_decision_targeting_proposal`
- `historical_decision_proposals_replay_and_remain_resolvable`
- `queue_decision_permits_open_ready_and_revisit_from_an_agent`
- `decide_decision_refuses_selected_option_or_decided_from_an_agent`
- `an_agent_created_decision_may_be_open_or_ready_but_never_decided`
- `revisit_at_creation_is_incoherent_and_refused`
- `ready_or_revisit_with_fewer_than_two_options_is_refused_at_admission`
- `plain_open_decisions_are_unconstrained`
- `a_direct_choice_is_legal_from_both_ready_and_revisit`
- `selected_option_and_decided_are_only_ever_written_by_a_decision_choice_patch`, including
  at node creation, where no author may start a Decision decided
- `decisions_awaiting_choice_matches_the_frontend_predicate`
- `a_seed_or_refresh_answer_is_persisted_and_readable`

## Failure means

An agent can decide rather than queue a Decision; a queued Decision cannot be
answered; a one-option ballot enters the Inbox; old append-only history stops
replaying; new ambiguity or Decision-Proposal records enter canonical history;
the backend and frontend count different Decisions; or a Seed/Refresh answer is
discarded before the human can read it.
