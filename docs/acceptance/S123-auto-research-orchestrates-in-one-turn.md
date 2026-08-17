---
id: S123-auto-research-orchestrates-in-one-turn
status: implemented
tier: hermetic
driver: pytest
covered_by:
  - tests/test_staged_command_client.py
  - tests/test_auto_research_commands.py
  - tests/test_auto_research_effects.py
  - tests/test_auto_research_stream.py
  - tests/test_auto_research_experiments.py
  - tests/test_auto_research_mail.py
  - tests/test_background.py
  - tests/test_experiment_loop_agent_io.py
  - tests/test_identity_patch_contract.py
  - tests/test_history_attribution.py
invariants: [3, 4, 4b, 8, 10, 10b, 10c, 10g]
---

# Auto-research repairs, reflects, and launches an Experiment in one turn

**Confirmed by the human 2026-08-16.**

An Auto-research orchestrator does not have to end a paid turn just to hand RCP
a Patch. It can apply a bounded graph change, read the refreshed graph, delegate
ordinary Work, and start a bounded Experiment-loop episode while the same native
orchestrator turn is still running. Those actions use the existing authority and
run paths; the staged client is a shell CLI for an authenticated tool-like API,
not MCP and not a second graph writer.

This scenario owns the in-turn command surface and its response contract.
[S124](S124-auto-research-harvests-child-lifecycle.md) owns what happens after
the admitted children change state and whether the parent may finish.

## UI path — decided 2026-08-16

There is no new human command console. The human still starts **Auto-research**
from the project header and observes the parent, spawned Work tasks, and child
Experiment episodes in Runs. `apply`, `spawn`, and `episode` are available only
inside the authenticated orchestrator stage. Provider, model, effort, execution
host, and another Auto-research episode are deliberately not selectable there.

## Setup

A project with one open Blocker, two ready Experiment nodes, an Auto-research
budget B of four, and therefore a shared orchestrator-started Experiment
allowance E of twenty. Human Settings provide one complete node Work profile. A
deterministic orchestrator can edit direct regular files in its reusable scratch
workspace and invoke the staged command client.

## Drive

1. Start Auto-research. Confirm both its initial prompt and a later continuation
   prompt name the same exact callable command surface, distinguish RCP lifecycle
   facts from agent mail, and tell the orchestrator to keep working until guarded
   finish succeeds or a true human-only dependency is named.
2. Write a valid `patch.json` that resolves the infrastructure Blocker and call
   `apply --key <key> patch.json`. Read the compact JSON response, reread the
   refreshed graph and research paths it returns, and continue in the same
   provider turn.
3. Change `patch.json` and repeat the first Apply key. Confirm the recorded result
   returns without reading or applying the new bytes. Use a new key for the new
   intent and confirm both dispositions remain ordered in the task result.
   Confirm unavailable attempts still count toward the 32-distinct-key turn
   allowance, same-key recovery does not, and concurrent contenders for the last
   place admit exactly one without reading the refused caller's file.
4. Submit a semantically invalid Patch. Confirm `invalid` leaves `patch.json` in
   place for correction. Correct it and Apply with a new key. Then Apply a valid
   empty Work Patch and confirm it consumes the file without spending a graph
   revision.
5. In a separate turn, leave a valid `patch.json` unconsumed. Confirm the existing
   end-of-turn settlement applies it once. In another turn, Apply the file in
   turn and confirm end-of-turn settlement cannot apply it again.
6. Write `worker-task.md`, then call `spawn` with that Experiment seat and
   `--instruction-file worker-task.md`. Confirm RCP snapshots the exact bytes and
   digest and starts an ordinary node Work task through the existing path.
7. Try a blank file, a nested path, an outside path, and a symlink for the worker
   instruction and for the Experiment goal. Confirm each fails closed without
   admitting a child.
8. Write a concise `experiment-goal.md` and call the
   `episode --kick-off-experiment` action for that Experiment with
   `--goal-file experiment-goal.md` and no invocation limit. Confirm RCP uses the
   human-configured node Work profile, pins the node's next-episode ceiling, and
   preserves the goal as the initial human message under the full RCP-owned
   Experiment contract.
9. Start another child Experiment with an explicit lower limit, then a legal
   higher limit. Confirm the option can move the child ceiling in either
   direction, while a value above total E is refused with E and a direct
   instruction to lower it. Confirm no provider, model, effort, or host option is
   accepted. Confirm an omitted node default above E is accepted but actual
   invocations remain curtailed by E.
10. Let invocations across both child Experiments interleave. Confirm every new
    allocation atomically spends one shared E unit and its own child ceiling;
    sleeping reserves nothing, exact Resume spends nothing, and E at zero admits
    no new child invocation or wake. Confirm an exhausted kickoff refuses before
    reserving a replacement or stopping an existing viable episode.
11. Attempt to start an Auto-research episode from the orchestrator command
    client. Confirm the closed command vocabulary has no such action.
12. Inspect the worker's initial and continuation prompts and a Patch-correction
    prompt. Confirm workers cannot spawn, start episodes, register a watcher, or
    wake themselves, and correction receives no orchestration surface.

## Assert

- `orchestrator_apply_reuses_the_live_work_apply_path`
- `orchestrator_apply_revalidates_current_state_under_the_append_lock`
- `apply_returns_revision_digest_messages_and_refreshed_state_paths`
- `an_apply_key_replays_its_recorded_result_without_reapplying_bytes`
- `an_interrupted_apply_commit_is_proved_by_its_effect_id_without_duplicating_a_revision`
- `multiple_in_turn_applies_are_ordered_and_watcher_visible`
- `invalid_apply_retains_patch_json_for_correction`
- `successful_and_valid_empty_apply_consume_patch_json`
- `end_of_turn_settlement_applies_only_an_unconsumed_patch`
- `applied_revision_and_singular_graph_update_remain_compatible_latest_values`
- `graph_updates_additively_records_every_in_turn_apply`
- `spawn_snapshots_one_direct_regular_instruction_file`
- `spawn_uses_the_existing_ordinary_node_work_path`
- `experiment_kickoff_snapshots_one_direct_regular_goal_file`
- `experiment_kickoff_preserves_the_optional_goal_message`
- `a_blank_experiment_message_uses_only_the_existing_fallback`
- `orchestrator_commands_do_not_expose_provider_model_effort_or_host`
- `omitted_invocation_limit_uses_the_node_setting`
- `explicit_invocation_limit_pins_the_requested_child_ceiling`
- `child_experiments_share_one_five_times_b_actual_spend_allowance`
- `exact_resume_does_not_spend_another_allocation`
- `an_auto_research_orchestrator_cannot_nest_auto_research`
- `root_prompts_expose_exactly_the_callable_surface`
- `worker_and_correction_prompts_do_not_inherit_root_commands`

## Tool-response boundary

Every operational command prints one compact JSON object. `status` is `ok`,
`invalid`, or `unavailable`; `message` is one plain-language sentence;
structured decisions live in `result`. An Apply success includes its
disposition, applied and live revisions, Patch digest, bounded validation
messages, and refreshed state paths. An episode response exposes the shared
Experiment allowance as total, used, and remaining, not launch-profile
internals.

The pre-existing `validate` display remains the compatibility exception with
`valid`, `invalid`, or `unavailable` plus `messages`; this scenario does not
force unrelated graph-agent validators to adopt the orchestration envelope.

One provider turn admits at most 32 distinct keyed Apply intents, including
intents whose effect returns `unavailable`. A same-key retry does not consume
another place. A later distinct Apply is `invalid` before RCP reads its file,
including under concurrent calls, making the task-result history bound explicit
rather than silently dropping an accepted disposition.

An intent that never reaches its effect is the one exemption: when RCP cannot
read the Patch file at all, no keyed intent exists to record, so the key and its
place both stay free for an exact later call and the failed read is kept as an
unkeyed audit attempt. Unreadable bytes are a transport failure, not a spent
Apply.

Current task consumers keep working: top-level `applied_revision` stays a scalar
and the singular `result.graph_update` stays the latest disposition. The ordered
`result.graph_updates` list is additive. No compatibility field changes type.

## Boundary

The goal and instruction files are task prose, not authority. The semantic Patch
is still the sole graph-change input, and Apply still goes through preparation,
current-state validation, the append lock, canonical history, and
materialization. This scenario adds an earlier time at which the existing path
may run; it adds no direct `.research` write path and no revision pin.

Prompt and tool support land atomically. Every command above is now callable,
and the shipped runtime prompt describes this exact surface rather than a
partially available future contract.
