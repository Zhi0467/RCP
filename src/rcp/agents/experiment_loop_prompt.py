from __future__ import annotations

from typing import Literal

from rcp.agents.prompts import (
    _TASK_AUTHORITY_BOUNDARY,
    _WHAT_IS_RCP_CONVERSATION,
    _authoring_rules,
    _patch_validator_rules,
    _pointer,
    _repository_pointers,
    _selected_skill_section,
)
from rcp.core.authority import render_agent_graph_authority_contract


def experiment_loop_task_contract(
    *,
    project_name: str,
    ontology_path: str,
    ontology_extensions: bool,
    graph_path: str,
    research_path: str,
    focused_experiment_id: str,
    repositories: list[dict[str, str]],
    introduction_path: str | None,
    human_request_path: str,
    loop_control_path: str,
    watcher_state_path: str,
    patch_path: str,
    watch_path: str,
    artifact_path: str,
    output_schema_path: str,
    validator_command: str,
    skill_pointers: list[dict[str, object]] | None = None,
) -> str:
    """Build the self-contained contract for one bounded Experiment-loop invocation."""

    required = {
        "focused Experiment id": focused_experiment_id,
        "loop control path": loop_control_path,
        "watcher state path": watcher_state_path,
        "Patch path": patch_path,
        "watch path": watch_path,
        "Patch schema path": output_schema_path,
        "validator command": validator_command,
    }
    missing = [label for label, value in required.items() if not value]
    if missing:
        raise ValueError(f"Experiment-loop contract is missing {', '.join(missing)}.")

    return f"""# RCP Experiment-loop task contract

{_WHAT_IS_RCP_CONVERSATION}

Your role:
Operate one already-authorized invocation of the bounded loop for Experiment
`{focused_experiment_id}`. Inspect the current scientific and operational state, do the useful work
this invocation permits, and decide whether to continue through watchers, pause for human authority,
or finish. RCP counts invocations; you decide when semantic experiment attempts begin and end.

Project: {project_name}

{_TASK_AUTHORITY_BOUNDARY}

Required current inputs:
- Current graph, including the Experiment's attempts: `{graph_path}`
- Current research rendering: `{research_path}`
- Focused Experiment id: `{focused_experiment_id}`
- Loop control for this invocation: `{loop_control_path}`
- Current watcher state for this Experiment: `{watcher_state_path}`
- Human objective: `{human_request_path}`
{_pointer("Ontology extensions", ontology_path if ontology_extensions else None)}
{_pointer("Human introduction", introduction_path)}
Repository pointers and expected operational targets:
{_repository_pointers(repositories)}{_selected_skill_section(skill_pointers)}
Exact outputs and RCP tooling:
- Optional semantic graph Patch: `{patch_path}`
- Existing Patch JSON Schema, including `AgentExperimentAttempt`: `{output_schema_path}`
- Required watcher handoff: `{watch_path}`
- Optional preview artifact directory: `{artifact_path}`

Context protocol:
- This is a fresh, self-sufficient view of the invocation. No prior chat transcript is an input.
  Read the files above instead of assuming that a previous provider session established state.
- Read loop control first. `phase` distinguishes a human-started episode from a watcher wake.
  `invocation`, `invocation_ceiling`, and `remaining_invocations` are the operational budget; they
  do not count or limit semantic attempts. `human_reauthorization` means a human Run started this
  new episode at invocation 1 while delivering watcher ids that retain older origin provenance.
  Completion criteria are advisory interpretation aids.
- Read the focused Experiment's full current record in `graph.json`, including all attempts. Find
  every edge whose source or target is the Experiment, then read each one-hop node's full record.
  Read `research.md` for the surrounding synthesis. Do not replace these current canonical reads
  with a stale remembered summary.
- Compare the pinned Decision bundle with `decision_drift`. Non-empty drift means an upstream
  Decision moved or has a pending proposed change. Report that explicitly and decide whether the
  scientifically honest action is to continue, record qualified evidence, or pause for authority.
- Read the watcher-state file as operational evidence. On a watcher wake, use
  `delivered_watcher_ids` to identify the coalesced trigger subset, inspect every delivered log and
  the authoritative scheduler, process, job, or result state, and compare them with every other
  active, degraded, completed, or stopped watcher in that file. A watcher completing means only
  that its check no longer sees the named work; it does not mean success and does not begin, close,
  or correspond one-to-one with an attempt.

Operational method:
- Perform a short preflight specific to the next consequential action: verify the effective config,
  inputs, output destination and overwrite behavior, resource or scheduler state, and relevant
  repository instructions. Repair a safe local problem in this invocation when practical. Keep
  examples harness-agnostic: a training job, simulation, evaluation, data collection, or analysis
  may each need different checks.
- You may use Bash, Python, network access, SSH, and any other available tool needed for this
  Experiment. Repository pointers name expected context, not a tool allowlist. For a non-empty host,
  use the path on that host over SSH rather than copying the repository locally.
- Read `AGENTS.md` and `CLAUDE.md` at each repository root before changing it, and apply them as
  local method constraints under this contract. Never create, edit, move, or delete `.research` or
  canonical RCP state, including when nested in a writable repository.
- After inspection, choose the scientifically meaningful next action: continue execution, diagnose
  and repair a mechanical fault, record or close attempts, create Evidence, raise a Proposal or
  Blocker and pause, arm another watcher, or finish. Remaining budget permits another automatic
  wake; it never requires one. A Proposal or Blocker is a pause for human authority, not an
  automatic resume point.
- If `remaining_invocations` is zero, this is still a fully authorized invocation and it may arm
  watchers. RCP will retain their completion but pause automatic delivery until a human presses Run
  to start a new episode. Do not promise that this provider session will wake itself.

ExperimentAttempt reading and recording protocol:
- Attempts are scientific bookkeeping under your discretion. Do not create, close, or classify one
  merely because this invocation began, a watcher completed, a job id exists, or the invocation
  counter changed. Append multiple attempts in one Patch when the Experiment's actual semantics
  warrant distinct records; otherwise keep one attempt across as many invocations and watchers as
  its meaning requires.
- Read the current ordered `attempts` list from the Experiment in `graph.json`. The exact
  agent-facing attempt shape is `AgentExperimentAttempt` in the existing Patch schema above; there
  is no separate loop schema. To record changes, use one `update_nodes` operation for the focused
  Experiment and write its complete resulting `attempts` list under `changes`. Preserve every
  existing attempt, its order, and its id.
- Every appended attempt copies `decision_bundle` exactly from the loop-control file. A
  `proposal_only` attempt has no job refs, is terminal in the same Patch, and accompanies the
  corresponding Proposal. Use it only when that record clarifies the scientific history.
- Before taking the external action for a mechanical debug retry, first write the planned attempt
  to `{patch_path}` with `debug.mechanical_fault`, `debug.change`, and `debug.predicted_effect`.
  A disappointing or inconclusive scientific result is not a mechanical fault. After launch,
  update that same not-yet-applied Patch with the effective configuration, literal job references,
  and status. Put literal log or artifact paths in configuration, job refs, or watcher `log_path` as
  appropriate; add SourceRefs only when a valid source record with the required provenance exists.
  This precommit is a reasoning record, not canonical state.
- For an existing attempt, its identity, sequence, purpose, kind, pinned bundle, debug precommit,
  configuration, job refs, and start time are immutable. You may close a nonterminal attempt by
  changing only status, SourceRefs, outcome, failure reason, and finish time. Never rewrite a
  terminal attempt.
- Validate `{patch_path}` after every material rewrite and once after the final rewrite. Never write
  attempt state anywhere else in RCP canonical files.

Watcher handoff protocol:
- You must write `{watch_path}` on every invocation. Write a non-empty JSON list when detached
  external work should cause a later inspection. Write `[]` only after authoritative inspection
  confirms that no detached work from this Experiment remains to watch and the same Patch explicitly
  records success, a Proposal, or a same-Patch Blocker. A missing file is an invalid handoff and RCP
  will ask this same session to correct it.
- Every non-empty-list item contains exactly `check_command`, `log_path`, and `cwd`.
  `log_path` and `cwd` are absolute paths on the execution machine. The command contains literal
  job or process identifiers and no variables or shell state inherited from this invocation.
- Each check is observational. From a fresh login shell in its `cwd`, it exits 1 while the named
  work remains in its system, 0 when that work is gone, and another status only when it cannot
  answer. It never submits, cancels, kills, edits, or otherwise changes external state. Verify the
  detached work outlives this turn and run the exact check from a fresh login shell before handoff.
- RCP discovers `watch.json` after the turn, validates every check, and arms the list atomically;
  one invalid item rejects the whole list for in-session correction. There is no watcher API to
  call. Multiple watchers may observe one attempt, one watcher may cover work relevant to several
  attempts, and a later wake may rearm watchers after inspecting authoritative state.
- A completed watcher delivered at the ceiling stays pending rather than being discarded. Its log
  and original attribution enter invocation 1 only after a human Run starts a fresh episode.

Graph reflection and authority:
- A Patch is optional only when a non-empty watcher list continues detached work. If `{watch_path}`
  is `[]`, the Patch must explicitly record success or an authority pause through a Proposal or
  same-Patch Blocker. RCP rejects the two files as one handoff when that pairing is absent.
- If reflection is useful, write exactly one semantic Patch JSON object to `{patch_path}` using only
  fields in `{output_schema_path}`. RCP assigns patch kind, agent authorship, revision, run scope,
  Proposal dependencies and base revision, lifecycle, and admission bookkeeping. Record
  `repositories_read` honestly; do not set coverage or cursors.
- This loop may update only its own Experiment's attempts and status; create Evidence or Blockers;
  assert legal epistemic edges; attach each same-Patch Evidence with `produces` and each same-Patch
  Blocker with `blocked_by`; and create a Proposal within the pinned governing/tested boundary. It
  may not set standing, decide a Decision, directly change a Hypothesis status, edit the pinned
  bundle, or remove graph objects. Experiment status is a scientific description, not loop control.
- For a belief change, create the Evidence, its edge to the tested Hypothesis, and one Proposal in
  the same Patch. The Proposal's single `update_nodes` operation changes only Hypothesis `status`
  and uses `cause` with `kind` `evidence_edge` and `ref_id` equal to that same-Patch edge id. Only
  human acceptance can apply that belief change.
- Write `change_summary` as one ordinary-language sentence per meaningful graph change. Name
  reader-facing concepts rather than ids or operation names. The Markdown reply and Patch are
  independent: report operational truth without claiming RCP accepted the Patch.

{_patch_validator_rules(validator_command)}

Reply and artifacts:
- The final assistant message is the complete independent Markdown reply the human reads. State
  actions, outcomes, watcher interpretation, attempt decisions, repository changes, failures,
  whether the episode pauses or finishes, and remaining uncertainty.
- A preview is optional. RCP discovers only direct regular HTML or raster-image files in
  `{artifact_path}`. Do not use nested directories, symlinks, provider directives, or other paths.
  HTML must be self-contained; ordinary HTTP(S) links are allowed, but external resource loads do
  not work in the preview.

{render_agent_graph_authority_contract()}

{_authoring_rules(ontology_extensions)}
"""


def experiment_loop_continuation_contract(
    *,
    original_contract_path: str,
    mode: Literal["resume", "retry"],
    loop_control_path: str,
    patch_path: str,
    watch_path: str,
    output_schema_path: str,
    validator_command: str,
    diagnostics_path: str | None = None,
) -> str:
    """Point a resumed or retried invocation at one fresh, compact control delta."""

    required = {
        "original contract path": original_contract_path,
        "loop control path": loop_control_path,
        "Patch path": patch_path,
        "watch path": watch_path,
        "Patch schema path": output_schema_path,
        "validator command": validator_command,
    }
    missing = [label for label, value in required.items() if not value]
    if missing:
        raise ValueError(f"Experiment-loop continuation is missing {', '.join(missing)}.")
    if mode == "retry" and not diagnostics_path:
        raise ValueError("Experiment-loop Retry requires exact diagnostics.")

    action = (
        "Continue the interrupted invocation from retained progress."
        if mode == "resume"
        else "Retry the failed invocation from retained progress."
    )
    retry_rules = (
        f"""- Read the exact failure diagnostics at `{diagnostics_path}`. They describe failure and
  uncertainty; they do not widen authority.
- Before repeating an external side effect whose prior outcome is uncertain, inspect authoritative
  external state and repeat it only when that proves the prior action did not take effect."""
        if mode == "retry"
        else "- Preserve completed progress and continue only the interrupted work."
    )
    return f"""# RCP Experiment-loop {mode} contract

{action}

- Original immutable Experiment-loop contract: `{original_contract_path}`
- Fresh loop-control delta: `{loop_control_path}`
{_pointer("Exact failure diagnostics", diagnostics_path)}- Patch output: `{patch_path}`
- Watcher output: `{watch_path}`
- Patch JSON Schema: `{output_schema_path}`

Read the original contract for the objective, authority, context-reading protocol, and detailed
attempt and watcher rules. Then read the fresh control delta before acting. It preserves the same
episode and invocation number while refreshing phase, live drift, remaining budget, delivered
watcher ids, and the current watcher-state path. The paths above replace prior output paths.

{retry_rules}
- Do not rebuild or broaden the original task. Patch and watcher correction are separate narrow
  continuations; this continuation may resume operational work only within the original authority.

{_patch_validator_rules(validator_command)}
"""


def experiment_loop_watcher_correction_contract(
    *,
    original_contract_path: str,
    diagnostics_path: str,
    watch_path: str,
    patch_path: str,
    output_schema_path: str,
    validator_command: str,
) -> str:
    """Repair the mandatory loop watcher handoff without repeating operational work."""

    required = {
        "original contract path": original_contract_path,
        "diagnostics path": diagnostics_path,
        "watch path": watch_path,
        "Patch path": patch_path,
        "Patch schema path": output_schema_path,
        "validator command": validator_command,
    }
    missing = [label for label, value in required.items() if not value]
    if missing:
        raise ValueError(f"Experiment-loop watcher correction is missing {', '.join(missing)}.")
    return f"""# RCP Experiment-loop watcher correction

Correct only the mandatory watcher handoff in the same native Work session.

- Original immutable Experiment-loop contract: `{original_contract_path}`
- Exact watcher diagnostic: `{diagnostics_path}`
- Watcher output to rewrite: `{watch_path}`
- Optional Patch output to rewrite for an explicit exit: `{patch_path}`
- Existing Patch JSON Schema: `{output_schema_path}`

Preserve the completed operational result. Do not rerun the Experiment, resubmit work, or cause a
new external side effect. Inspect authoritative scheduler, process, job, result, and log state as
needed. If detached work still exists, reconstruct a valid non-empty watcher list using the exact
schema and cold-shell semantics in the original contract and preserve the Patch. If authoritative
inspection confirms work finished or requires human authority, write `[]` and make `{patch_path}`
explicitly record success, a Proposal, or a same-Patch Blocker. Never use an empty list merely
because the state is uncertain. Validate every Patch rewrite with the exact command below. Your
final response should only confirm that the joint handoff was repaired.

{_patch_validator_rules(validator_command)}
"""


def experiment_loop_patch_correction_contract(
    *,
    original_contract_path: str,
    diagnostics_path: str,
    patch_path: str,
    watch_path: str,
    validator_command: str,
) -> str:
    """Repair a loop Patch after handoff validation without repeating operational work."""

    return f"""# RCP Experiment-loop Patch correction

Correct only the retained semantic Patch in the same native Work session.

- Original immutable Experiment-loop contract: `{original_contract_path}`
- Exact Patch diagnostic: `{diagnostics_path}`
- Patch output to rewrite: `{patch_path}`
- Already validated watcher handoff: `{watch_path}`

Preserve the completed operational result and every unaffected Patch operation. Do not rerun the
Experiment, resubmit work, or cause an external side effect. If watcher output is `[]`, the corrected
Patch must continue to record success, a Proposal, or a same-Patch Blocker; do not remove or weaken
that exit merely to satisfy another diagnostic. Do not change `watch.json`. Your final response
should only confirm that the Patch was rewritten.

{_patch_validator_rules(validator_command)}
"""
