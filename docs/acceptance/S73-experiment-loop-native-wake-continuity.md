---
id: S73-experiment-loop-native-wake-continuity
status: implemented
tier: hermetic
driver: pytest
covered_by: tests/test_experiment_loop_agent_io.py,
  tests/test_experiment_stop.py, tests/test_transport.py,
  tests/test_acceptance_agent.py, tests/test_control.py, tests/test_prompts.py
invariants: [4, 4b, 8, 9, 10b, 10c, 10d]
reported_by: human, 2026-08-06
last_passed: 2026-08-06 — full backend suite plus served-app initial Run and
  automatic watcher wake verified one episode-native session; legacy stop
  recovery was reconciled in the live CRLP ledger and a fresh UI Run reached an
  active agent turn; race, recovery, and remote-stage edge paths were inspected
  and covered by focused tests; validator, materialization, and prompt coverage
  passed for focused summary and next-action authority.
---

# Watcher wakes continue one bounded episode session

Confirmed by the human on 2026-08-06.

A human **Run** starts a fresh bounded episode and native Codex or Claude
session. Automatic watcher wakes inside that episode create new durable RCP
tasks and consume the next invocation from the episode budget, but resume that
episode's native provider session. This preserves operational continuity without
letting native context grow across human authority boundaries: every later human
Run starts a fresh session and episode, including Proposal/Blocker resolution,
invocation-limit reauthorization, and restart after S72 **Stop loop**.

An automatic wake is not task-level Resume. Task Resume continues one paused
RCP task at the same invocation number. A watcher wake is a new RCP task with
`trigger="watcher"`, the next invocation number, a separately persisted answer
and handoff, and the same episode-native provider session.

This changes the current blueprint rule that an **Experiment-loop** watcher wake
uses a fresh provider session. Generic Work watchers remain fresh Work turns as
promised by S42. The implementation must update the canonical blueprint in
place, bump its internal version and changelog, and update the affected
implemented clauses in S41. No amendment or retained blueprint snapshot is
allowed.

## Session, phase, and provider matrix

| Trigger | Episode | Native provider session | Agent-facing phase and prompt | Resolved provider configuration |
|---|---|---|---|---|
| First human Run, or human Run after Proposal/Blocker resolution with no completed ungrouped watcher or ready group | new, invocation 1 | fresh | `initial_run`; full Experiment-loop contract | current Node-chat profile and selected/default truth scope |
| Automatic ungrouped completion or group readiness below the ceiling | same, invocation N+1 | resume the current episode session | `watcher_wake`; compact continuation below | pinned by the episode session |
| Task Pause → Resume | same, same invocation | resume the same episode session | `resume`; existing compact task-resume contract | unchanged |
| Task Retry/correction | same, same invocation | existing explicit task-recovery semantics | `retry` or correction; existing narrow contract | unchanged |
| Human Run delivering completed watcher state after exit or ceiling | new, invocation 1 | fresh | `human_reauthorization`; full Experiment-loop contract | current Node-chat profile and selected/default truth scope; watcher configuration remains provenance |
| Human Run after S72 Stop loop | new, invocation 1 | fresh | `initial_run`; full Experiment-loop contract with stopped history and no delivery | current Node-chat profile and selected/default truth scope |

Every episode has exactly one validated native-session binding: provider,
session id, execution host, and exact reusable chat stage. Every automatic wake
resumes that binding, regardless of which invocation armed its delivered
watchers or which permitted Work conversation later maintained them. Compatible
cross-invocation, cross-conversation, and older-episode watcher provenance may
coalesce, but watcher provenance never chooses or changes the native session;
the newest human-authorized episode does.

An automatic wake may claim only a completed ungrouped watcher or ready watcher
group attached to the same Experiment whose check still runs on the episode's
execution host. Origin conversation, provider, machine alias, truth scope, and
package selection are provenance rather than selectors; the live episode owns
the wake policy and session. A stale node or episode, stopped loop, missing
durable binding, or wrong check host remains visible and cannot silently switch
sessions, consume budget, or become a generic Work wake. A later human Run may
explicitly reauthorize compatible pending watcher state into a fresh episode.

Before atomically claiming a completed ungrouped watcher or ready group or
spending the next invocation, RCP validates that the episode session and exact
saved stage still exist on the pinned execution machine. A transiently unavailable binding leaves
the watchers completed and unnotified for a later delivery pass. A missing or
mismatched binding becomes an exact Needs-action diagnostic in S72 and never
silently launches a fresh session. The human may restore availability, or use
**Stop loop** and a new Run to cross that authority boundary explicitly.

Provider, model, reasoning, execution machine, truth scope, and authority stay
pinned for the episode. Current graph/research/schema/output pointers are
refreshed every turn. If ontology, repository pointers, enabled package
pointers, or other task context changed since the preceding successful loop
turn, the wake appends one compact exact replacement block and commits it as the
episode baseline only after a mechanically successful joint Patch/watcher
handoff. A graph-level rejection is recorded truthfully and does not erase an
otherwise accepted operational handoff. Unchanged context is omitted;
an empty replacement block renders nothing rather than a heading with “none.”

## Watcher storage and per-turn access

No MCP or provider tool owns watcher authority. A conversation's `watch.json`
retains the strict observer-only self-wake from S42. An Experiment's separate
watcher file contains strict observer items, optional group labels, and reasoned
retirement items for staged compatible observers; the loop and every permitted
maintenance Work turn write that same node resource. The file path decides which
target wakes, without a discriminator or agent-supplied target field. RCP
validates the complete list after the turn and persists each watcher as a durable SQLite
`WatcherRecord`, separate from graph state, chat history, semantic
ExperimentAttempt records, and provider task lineage. The record retains:

- watcher id, project, Experiment, accepting episode, creation conversation,
  and originating operation;
- execution host, absolute `cwd`, observational `check_command`, and absolute
  `log_path`;
- `active`, `degraded`, `completed`, or `stopped` status, check timestamps, next
  due time, consecutive-error count, exit code, error, completion time, delivery
  claim, notification task, and any agent-retirement reason and time;
- immutable Experiment-group identity and membership when present;
- the frozen continuation envelope: provider/model/reasoning/machine, truth
  scope, package selection, Patch authority, originating episode/invocation,
  ceiling, control revision, pinned decisions, and completion criteria.

The agent never reads SQLite. Before each loop turn and each conversation that
may inspect the node resource, RCP stages a bounded
`experiment-watchers.json` in the exact local or remote scratch workspace and
points to it from the applicable contract. A Work turn receives the writable
Experiment file and its loop wake target; Discuss receives readable state only.
Each item contains the
operational fields the agent can act on: id, origin operation, execution host,
check, log, cwd, status and timestamps, last exit/error, notified claim, origin
episode/invocation/ceiling/revision, and pinned decision bundle.

The staged selection is explicit. It includes group identity and every member's
status, error, and consecutive-error count, so a diagnostic group wake preserves
unknown external outcomes rather than reporting degraded work as failed:

- **Automatic wake:** every delivered ungrouped watcher or every member of each
  delivered group even after its atomic claim, plus other active/degraded and
  completed-unnotified watchers relevant to the Experiment.
  `delivered_watcher_ids` and `delivered_watcher_groups` identify the trigger
  subset and group membership.
- **Fresh initial Run:** `delivered_watcher_ids=[]`; include relevant
  active/degraded and completed-unnotified observers plus watcher records from
  the immediately preceding human-stopped episode.
- **Human reauthorization:** include the atomically claimed completed watcher or
  ready group even though it now has `notified=true`, plus the other relevant
  observers.
- **Stopped records:** retain and stage them only when they belong to the
  current episode or the immediately preceding S72 human-stopped episode needed
  by a fresh Run. They are context, never triggers.

The current graph and `research.md` are staged or pointed to independently on
every turn. All prompt shapes therefore contain both semantic graph context and
operational watcher context; only their live contents and delivered subset
differ. Prior chat transcript is never an input.

## Automatic wake message

The original provider session already holds the immutable full Experiment-loop
contract. An automatic wake sends the following short human-style continuation
message. It confirms what RCP accepted from the preceding loop turn, names the
delivered watchers, replaces stale file pointers with fresh ones, and restates
the asynchronous mechanism and all three valid paths from this turn. It does
not rebuild the master contract.

```text
The watched work for Experiment `{focused_experiment_id}` is ready for another look. Continue the
same bounded loop in turn {invocation} of {invocation_ceiling}.

RCP accepted the previous turn's handoff:
- graph update: {previous_graph_result}
- watcher dispositions: {previous_watcher_ids_or_none}

This turn was triggered by: {delivered_watcher_ids_or_groups}

If this is an immutable watcher group, its staged state names every member and
the group is ready only because none is still observed active. Exit-`0` members
are only gone; degraded members have unknown external state and must be
inspected before you relaunch, cancel, or record an outcome.

A completed watcher means only that its check no longer sees the named external work. It does not
mean the work succeeded and does not begin, close, or correspond one-to-one with a scientific
attempt. Inspect its authoritative scheduler or process state and its logs before interpreting the
result. If it refers to work that was already submitted, inspect that work; submit a replacement
only when the authoritative state shows that the earlier submission did not start, or after you
have recorded the specific mechanical fault and changed relaunch plan required by the Experiment
attempt protocol.

Read the fresh state before acting:
- loop control: `{loop_control_path}`
- watcher state: `{watcher_state_path}`
- current graph: `{graph_path}`
- current research rendering: `{research_path}`
- Patch output: `{patch_path}`
- watcher output: `{watch_path}`
- Patch JSON Schema: `{output_schema_path}`
- Patch validator: `{validator_command}`
{context_replacement_block_or_nothing}

For this turn, take whichever path matches the operational state:

1. Detached work remains or you have useful debugging and relaunching work to do.

   Continue the work that is useful now. Use watchers for detached work that will outlive this
   turn—typically a SLURM or other scheduler job, a long build or compilation, a long evaluation,
   data collection, simulation, or another process expected to take at least ten minutes. You may
   write multiple watchers. Write `{watch_path}` as:

   [
     {
       "check_command": "ids=$(squeue -h -o '%A') || exit 2; grep -Fxq 48192 <<<\"$ids\"; case $? in 0) exit 1;; 1) exit 0;; *) exit 2;; esac",
       "log_path": "/absolute/path/to/job-48192.log",
       "cwd": "/absolute/path/to/repository"
     }
   ]

   An ungrouped observer object has exactly `check_command`, `log_path`, and `cwd`; an
   Experiment observer may also carry one non-blank `group` label. An Experiment may retire a
   staged compatible observer with `{"stop_watcher_id": "...", "reason": "..."}` after it has
   settled the external work itself. From a cold login shell in `cwd`, the check exits 1 while the
   named work remains, 0 when it is gone, and another status only when it cannot answer. Verify
   the literal check before writing it. Once the useful synchronous work and handoff are complete,
   do not wait or poll for detached work; finish this turn. RCP validates the file, monitors
   accepted watchers, and resumes this episode session when a watcher or group is ready.

2. You need human input.

   Use this path when an upstream Decision is under- or over-specified, when you have a concrete
   permitted Decision or Hypothesis change for human approval, or when a scientific, design,
   implementation, data, or infrastructure blocker cannot be resolved without human action. Write
   one Patch at `{patch_path}` using the exact schema at `{output_schema_path}`, then run
   `{validator_command}`.

   For a concrete permitted human decision, use `create_proposals`. Its nested operation may change
   only the allowed Decision `selected_option`/`status` or Hypothesis `status` fields. Fill the
   Proposal's `card.situation_cold`, `why_human_now`, `consequences`, and `decision_needed` so the
   human can decide without reconstructing this turn.

   When the needed design change cannot be represented by that narrow Proposal authority, create
   an open `blocker` with `create_nodes` and connect this Experiment to it with a same-Patch
   `blocked_by` edge. Experiment-loop authority cannot add a `requires_decision` action edge, so
   identify any relevant Decision precisely in the Blocker's description, resolution condition,
   and recommended human action instead.

   If detached work still deserves observation while the human decides, write a non-empty
   `{watch_path}` using path 1's exact watcher format. Those watchers continue observing, but the
   Proposal or Blocker exits this episode, so they cannot automatically wake it; a later human Run
   may reauthorize completed watcher state. If no detached work remains, write `{watch_path}` as
   `[]`.

3. The Experiment is operationally finished.

   This means no detached mechanical work remains; the scientific result may be successful,
   unsuccessful, inconclusive, or invalid. Write `{watch_path}` as `[]`. At `{patch_path}`, write a
   schema-valid Patch that updates this Experiment's `status` to `completed`, preserves and closes
   its attempts truthfully, and creates any warranted Evidence, edges, or human-authority Proposal.
   Experiment-loop authority may update only this Experiment's `status`, complete `attempts` list,
   `current_summary`, and `next_action`. When the invocation introduces or closes attempts or changes
   what should happen next, keep those two prose fields consistent with the resulting attempt ledger
   and actual next step; leave them unchanged when still accurate, and use `next_action: null` when no
   further action remains. Put scientific outcomes in the relevant attempt, Evidence, and Markdown
   reply rather than treating the summary as a substitute. A minimal mechanical completion is:

   {
     "summary": "Finished the Experiment's operational work.",
     "ops": [
       {
         "op": "update_nodes",
         "nodes": [
           {
             "id": "{focused_experiment_id}",
             "changes": {
               "status": "completed"
             }
           }
         ]
       }
     ],
     "repositories_read": [],
     "change_summary": ["Finished the Experiment's operational work."]
   }

   Extend that Patch rather than omitting scientifically necessary attempt closure, Evidence, or
   interpretation, but remain within the original Experiment-loop authority. Validate it with
   `{validator_command}`.

Your Markdown reply remains independent from `patch.json` and `watch.json`. State what you found,
what you changed or launched, which path you took, and any remaining uncertainty.
```

The message deliberately says **turn**, not invocation. Invocation remains the
internal persisted budget term and API field.

After RCP applies that Patch, the human path **Runs → expand the Experiment →
Experiment meaning** immediately shows the updated **Current summary** and
**Next action** beside the canonical attempt ledger.

## Drive

1. Start a bounded Experiment with a fake Codex provider that records its native
   session and arms two watchers. Repeat the provider-specific command assertion
   with Claude.
2. Complete one watcher. Drive its automatic wake, then arm replacement work.
   Confirm a new RCP task and budget unit but the same episode-native session.
3. Complete a watcher from an older invocation together with one from the latest
   invocation. Confirm one coalesced wake resumes the current episode session.
4. Drive a wake that exits through a Proposal while retaining a watcher. Confirm
   that watcher continues observation but cannot wake the exited episode.
5. Resolve the Proposal and press Run before that watcher completes, then in a
   separate fixture after it completes. Confirm initial Run versus human
   reauthorization behavior from the matrix.
6. Reach the invocation ceiling with pending completion and press Run. Confirm
   a fresh session and `human_reauthorization` turn 1.
7. Use S72 Stop loop, then Run. Confirm a fresh initial session with stopped
   history and no delivered watcher ids.
8. Make the current episode session transiently unavailable, then permanently
   mismatched, before automatic claim.
9. Resume and Retry a paused pre-migration invocation whose root task has no
   retained episode-context candidate. Confirm RCP refuses the impossible
   continuation without launching another provider process, records why the
   episode cannot continue, and lets **Stop loop** preserve the task history,
   abandon only that recovery path, settle the episode, and enable **Run** for a
   fresh episode.
10. Apply an Experiment-loop Patch that closes an attempt, refreshes
    `current_summary`, and clears `next_action`. Confirm all three changes
    materialize together, while another Experiment field and a foreign-node
    prose update remain rejected.

## Assert

- `human_run_starts_fresh_native_session_and_episode`
- `initial_run_uses_current_node_chat_profile`
- `human_reauthorization_uses_current_node_profile_and_new_chat`
- `automatic_wake_is_new_task_and_budget_unit_but_resumes_episode_session`
- `task_resume_is_same_task_lineage_and_same_invocation_not_a_wake`
- `codex_and_claude_automatic_wakes_use_native_resume_without_task_resume_semantics`
- `episode_binding_pins_provider_session_host_and_exact_stage`
- `watcher_provenance_never_selects_the_resumed_session`
- `compatible_cross_invocation_watchers_coalesce_into_current_episode_session`
- `stale_episode_or_wrong_host_completed_watchers_remain_pending_and_visible`
- `automatic_wake_never_switches_episode_provider_machine_or_scope`
- `session_preflight_precedes_watcher_claim_and_budget_spend`
- `transient_session_unavailability_leaves_watchers_unnotified`
- `missing_or_mismatched_session_is_visible_and_never_falls_back_silently`
- `legacy_missing_context_candidate_stops_cleanly_and_enables_fresh_run`
- `stop_then_run_is_the_explicit_fresh_session_recovery`
- `watchers_remain_sqlite_operational_records_not_graph_or_transcript`
- `agent_reads_bounded_staged_watcher_json_not_sqlite`
- `watcher_selection_matches_wake_initial_reauthorization_and_stop_rules`
- `every_prompt_shape_has_fresh_graph_and_watcher_context`
- `prior_chat_transcript_is_never_an_input`
- `wake_uses_compact_human_message_not_rebuilt_master_contract`
- `wake_confirms_previous_patch_and_watcher_handoff`
- `wake_exposes_the_exact_live_patch_validator_command`
- `wake_replaces_changed_repository_ontology_and_package_context_only`
- `context_replacement_baseline_commits_only_after_successful_turn`
- `wake_explains_completion_as_observation_not_success_or_attempt_boundary`
- `wake_gives_precise_inspect_before_relaunch_rule`
- `wake_names_all_three_exit_paths_and_exact_watcher_shape`
- `human_authority_path_is_schema_and_authority_truthful`
- `proposal_or_blocker_may_coexist_with_observational_watchers`
- `operational_completion_does_not_claim_scientific_success`
- `loop_attempt_changes_may_refresh_summary_and_next_action`
- `wake_says_turn_not_invocation_to_agent`
- `valid_async_handoff_tells_agent_to_finish_instead_of_polling`
- `invalid_patch_or_watcher_handoff_still_uses_same_session_correction`

## Failure means

An automatic wake starts a fresh provider session, consumes no budget, or is
misreported as task Resume; a human Run inherits an old native session; watcher
origin chooses the wrong session; a missing session spends budget or silently
falls back; a legacy task with no retained episode context remains permanently
retryable while its graceful stop can never settle; watcher state becomes graph
or transcript input; the continuation
rebuilds static instructions but omits live state; or its exit guidance implies
completion means success, encourages polling, or permits duplicated external
work without authoritative inspection; or an invocation changes the attempt
ledger or next step but cannot keep the focused Experiment's visible summary
and next action consistent with that canonical result.
