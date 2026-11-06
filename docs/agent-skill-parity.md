# Same skill loading for every agent

## Outcome

Extend the existing project Work skill path to every task-execution agent. A project
owns its optional official skill/workflow selection through saved Project
Settings. Space uses `SkillDefaults()` because it has no project. These sources
are the only difference in discovery, staging and explicit invocation.

Keep workflows. Space's existing default offers all official skills and no
workflow. An explicit empty project selection remains empty. Required internal
methods remain required: graph consolidation invokes `graph-consolidation`, and
an episode report invokes `episode-report`, alongside the project's optional
catalogue. Their dependencies remain available to that internal task.

This changes official RCP packages. Provider-native discovery continues to use
the provider account and its existing inventory; no native-skill settings or
provider launch flags change. No UI layout, database schema, credential,
deployment, or graph-authority change is needed.

Session-summary calls remain exempt: they are internal transcript transforms
whose fixed capability cannot read package files. Do not widen that capability.

## Current gaps

Ordinary Work, Discuss, Paper coach, Experiment and child Work already resolve
the project's saved selection and stage it with `stage_skill_selection`.
Space stages its defaults only after an explicit invocation, so a normal turn
receives no catalogue. Seed/Refresh and the orchestrator stage the catalogue but
omit structured invocation pointers; their continuation metadata also omits
descriptions. The seated Auto-research worker path omits packages. Graph/code
merge and revoking artifact-edit turns receive no catalogue. Consolidation and
episode reports replace the optional catalogue with only their required method.

The runtime regression was reproduced before implementation: a real Space turn
with no projects reaches the provider with no `skills` in its recorded master
values. The same omission exists when projects are present.

## Shared contract

1. Resolve optional official IDs from saved project Settings or Space defaults
   afresh for each turn or task attempt, including Resume and Retry. Repeated
   provider passes within that attempt share its resolved selection. Ignore
   client-supplied `skill_ids`,
   `workflow_ids` and recorded `resolved_skill_packages` as selection authority.
   Deduplicate IDs while preserving order and resolve workflow dependencies
   through the existing registry.
2. Validate explicit `invoked_skill_ids` and `invoked_workflow_ids` against that
   optional selection. Unknown or disabled IDs fail before a provider starts;
   repeated IDs invoke once. Keep the human message bytes unchanged. A required
   internal method is added by its owning task after public invocation
   validation; it does not become invocable through disabled project Settings.
3. Stage the resolved union with the existing immutable, content-addressed
   bundle and its existing safety checks. Pass descriptions and exact paths to
   the agent. Package bodies remain file-backed, and unchanged selections reuse
   their safe bundle.
4. Use the existing description-matching rule for automatic discovery and
   `invoked_package_pointers` plus its renderer for explicit invocation. Invocation
   is per turn, outside the retained master; a later turn does not inherit a
   previous human's explicit invocation. A task's required method is invoked on
   each provider pass of that task, corrections included.
5. Put full compact catalogue metadata in retained-master current values so
   additions, removals and changed descriptions/versions/paths reach resumed
   sessions through the existing delta. Do not rewrite recorded masters or
   canonical history. Rebootstrap only when the owner's stable framing changes.

Skills supply methods, never permissions. Merge keeps its exact worktree and
answer/check contract. Artifact edits keep their origin/session binding and
scratch-only Discuss enforcement. Reports keep their frozen compact receipt,
session, stage, output and mandatory-skill version check; this change does not
remove the existing report-version refusal or import ancestor invocations.

Project Settings is read again on the next turn or attempt, including an
Experiment watcher wake. It does not alter an already running attempt. This
matches current selection code but contradicts the watcher spec's statement that
workflow/skill packages are pinned Experiment policy. Reconcile that sentence
with this explicit Settings rule; keep the episode's pinned provider, machine,
authority, native session, stage, completion policy and budget unchanged.

## Alternatives

- Extending the existing launch owners is selected: each already knows its
  catalogue source, exact stage, retained master and authority boundary.
- Central provider-launcher injection is rejected: the launcher does not own
  project Settings or task-specific required methods, and injecting there would
  bypass origin-bound and recorded-result lifecycle decisions.
- Invocation-only staging is rejected: it preserves the Space bug by leaving
  ordinary turns unable to discover methods from their descriptions. Embedding
  every package body is also rejected; the existing immutable file pointers
  already support both discovery and explicit reading.

## Detailed implementation plan

### Shared selection and prompt metadata

Modify `src/rcp/skill_registry.py` to expose the invocation validation currently
inside `ProjectService.resolve_skill_request`. Accept an allowed `SkillSelection`
and the two invocation lists, verify registry identity and membership, and return
the two normalized lists as `tuple[list[str], list[str]]` in workflow/skill order.
Use `SkillRegistry.validate_invocations(selection, *, workflow_ids, skill_ids)`;
both lists are required and errors remain `ValueError`. Both project and Space
admission use it. No package
import mechanism, configuration selector or new runtime owner is introduced.

Modify `src/rcp/service.py`: extend `resolve_skill_selection` with explicit
keyword-only `required_workflow_ids: Sequence[str] = ()` and
`required_skill_ids: Sequence[str] = ()` arguments. Resolve the saved
optional IDs plus these task-owned requirements through the existing registry.
`resolve_skill_request` still validates public invocations against the optional
selection only and records exact references as the existing launch receipt does.

Modify `src/rcp/agents/prompts.py` to share the existing staged-package usage rule
and `skill_prompt_values(pointers: list[dict[str, object]] | None) -> list[dict[str, object]]`
with `id`, `kind`, `label`, `version`, `description`, `dependencies` and `path`
metadata. Keep the existing invocation renderer and message boundary. Modify `src/rcp/runs/chat.py` to use the shared catalogue-value function.
Existing project Work/Discuss behavior stays the reference implementation.

### Space

Modify `src/rcp/runs/space_chat.py`: resolve and stage all defaults once for every
prompt build, including an empty Space, wakes, Resume and Retry. Pass the same
pointers to discovery metadata and to explicit invocation filtering; remove the
invocation-only staging branch. Add top-level `skills` to Space master values,
outside per-project context, and use the shared description-matching rule in the
fixed contract. Bump `SPACE_MASTER_POLICY` so pre-fix sessions open the amended
contract. Keep skill metadata out of the fixed-part digest so package changes use
the existing current-values delta. Modify `src/rcp/api/agent_threads.py` to persist
the normalized invocation lists returned by the shared validator.

### Project launch owners

Modify these existing owners, without changing their capability or output path:

| Files to modify | Change |
| --- | --- |
| `src/rcp/runs/tasks/graph.py` | Keep Settings staging; include complete compact skill values and render this task's explicit invocation in initial and resumed/Retry/correction launch parts. It must not become a sticky master instruction. |
| `src/rcp/runs/tasks/auto_research_stream.py`, `src/rcp/agents/auto_research_prompt.py` | Keep root Settings staging, add it to the seated worker path, include complete catalogue values, and add this task's invocation to fresh and continuation parts. Existing child Work continues using shared Work staging. |
| `src/rcp/runs/auto_research_delivery.py`, `src/rcp/runs/auto_research_admission.py` | Clear inherited `invoked_skill_ids` and `invoked_workflow_ids` when constructing a new lifecycle, mail, watcher or continued-root allocation from `current.request`. Keep invocations on Retry of the same allocation. A newly captured human turn uses only its own structured invocation, never the previous turn's. |
| `src/rcp/runs/tasks/experiment_loop.py`, `src/rcp/agents/experiment_loop_prompt.py`, `src/rcp/runs/tasks/coach.py` | Replace independent reduced skill-value maps with `skill_prompt_values`, retaining existing Settings staging. Experiment `_compose_fresh_prompt` must build its retained contract with `invoked=False`, then append `_turn_invocations` in the fresh turn envelope as recovery already does. Coach `render_contract` must omit invocation pointers from the retained master; append the current attempt's invocation separately to initial launch parts and `_coach_continuation_parts` for Resume/Retry as well as human turns. Bump `EXPERIMENT_LOOP_POLICY_VERSION` and `PAPER_COACH_POLICY_VERSION` so legacy masters containing an invocation rebootstrap with an invocation-free contract. Additions, removals and changed descriptions reach resumed/watcher sessions through current values. |
| `src/rcp/runs/consolidation.py`, `src/rcp/consolidation.py`, `src/rcp/runs/tasks/work.py` | Resolve Settings plus required `graph-consolidation`; append its required invocation rather than replacing optional packages or valid invocations. Admission and runtime must record the same union. Change `_compose_work_correction_prompt` and its refused-session fresh fallback as well as the initial consolidation launch: render the required pointer outside the retained master on each pass. The server-owned consolidation binding remains the only source of the required invocation. |
| `src/rcp/runs/tasks/episode_report.py`, `src/rcp/agents/episode_report_prompt.py` | Perform the existing frozen mandatory-skill check first; resolve Settings plus required `episode-report`, stage the union, advertise optional methods and explicitly invoke the required report package. Locate its exact pointer by kind/id, rather than asserting there is only one staged package. Keep `EpisodeReportRunRequest` and wrapup storage unchanged. |
| `src/rcp/runs/tasks/branch_merge.py`, `src/rcp/runs/branch_merge.py`, `src/rcp/agents/branch_merge_prompt.py` | Before an actual graph/code provider pass, resolve Settings and stage on the exact merge stage. Pass pointers to both contract builders and current values. Reuse the existing invocation renderer for any admitted current-turn invocation; do not invent new merge UI fields. Recorded finalization that launches no provider requires no new bundle. |
| `src/rcp/runs/tasks/artifact_edit.py`, `src/rcp/runs/artifact_edit_admission.py`, `src/rcp/runs/tasks/discuss.py`, `src/rcp/api/tasks.py` | In `admit_artifact_edit`, normalize after binding the actual origin profile: discard client selection/reference overrides and native fields, and validate official invocation IDs through `service.resolve_skill_request`. Remove later official-invocation clearing in the chat ingress. Retry/Resume admission validates retained IDs without rebinding its frozen profile. Before every actual Discuss/revoking launch, resolve current Settings and validate retained invocation IDs again; stage the result and use local normalized lists for prompt rendering. Keep the frozen request unchanged at runtime: its finalization contract must still equal the stored request on recovery. Record actual resolved references through launch-receipt extra fields, not by rewriting that request. Thus BackgroundTasks may reconstruct the original request on Retry without introducing a new service owner. Add the immutable inputs read root to revoking provider grants. Origin-bound Discuss includes current skill metadata in its delta while retaining exact protected master bytes and revoked operational commands. |

For retained masters, use the common catalogue value shape. Older master values
remain immutable: a shape change is sent as a current `skills` replacement or a
normal policy rebootstrap. Bump the affected owner policy constants when their
stable framing changes. Remote owners finalize staged inputs before the provider
uses them and expose immutable package inputs through their existing read roots.
Do not stage or reread optional packages to settle an already consumed result.
Apply the same rule to a recovered consumed pass: settlement uses its recorded
contracts and never resolves today's Settings. No change to `src/rcp/background.py`
is needed because actual artifact runtime validation is authoritative.

### Verification and documentation

Add regression cases in these existing tests; do not create a parallel launcher
or a real-provider test dependency:

| Test files to modify | Required evidence |
| --- | --- |
| `tests/test_skill_registry.py`, `tests/test_provider_skills.py` | Explicit project subset/empty selection, normalized invocation, disabled/unknown refusal, dependency closure, and unchanged provider-native behavior. |
| `tests/test_space_chat_runtime.py`, `tests/test_agent_thread_routes.py`, `tests/test_session_master.py` | A first Space turn with zero/multiple projects receives every default description and readable immutable pointer without invoking anything. Chosen invocation points at its staged package; the next uninvoked turn has none. Unchanged bundles reuse; changed catalogue metadata reaches retained and pre-fix sessions. Space defaults are independent of project selections. |
| `tests/test_api.py`, `tests/test_direct_ingestion_run.py`, `tests/test_direct_ingestion_contract.py`, `tests/test_auto_research_stream.py`, `tests/test_auto_research_delivery.py` | Seed/Refresh and root/seated workers discover only Settings packages and receive exact per-task invocation through fresh, resumed and correction paths. Initial invoked root to new lifecycle/mail/watcher/continued-root allocations clears the invocation; Retry of the original allocation retains it. Settings changes during a running correction sequence wait for the next turn/attempt. |
| `tests/test_experiment_loop_agent_io.py`, `tests/test_coach_provider_scope.py`, `tests/test_coach_session_switch.py` | Inspect retained masters to prove they contain no explicit invocation. Cover an invoked turn followed by an uninvoked turn/wake, invoked Resume/Retry, and rebootstrap of a legacy invocation-bearing master. Added/removed selections and changed descriptions reach resumed Experiment, watcher and coach turns. Retain pinned Experiment execution policy and empty-selection behavior. |
| `tests/test_consolidation_work.py`, `tests/test_consolidation_runtime.py`, `tests/test_episode_report.py`, `tests/test_episode_report_prompt.py` | Optional subset/empty catalogue plus required method/dependencies; inspect exact required invocation separately on initial, correction and refused-session replacement passes. Keep report pin/version refusal, no ancestor invocation, output, Stop and authority fences. |
| `tests/test_branch_merge_api.py`, `tests/test_episode_merge.py`, `tests/test_branch_merge.py`, `tests/test_artifact_edit_runtime.py`, `tests/test_artifact_edit_admission.py` | Graph/code merge and local/remote edit stage Settings packages, retain existing capabilities/roots/output verification, reject forged selections and disabled invocations, and preserve consumed-result settlement without optional staging. |

Verify every test path exists before editing. Extend the nearest existing tests
for these paths rather than adding test scaffolding. Run affected Python checks
serially, Ruff, pre-commit/basedpyright/comment ratchet, and scoped crapper/dryer
and mutation checks. Full suites belong to PR CI. Run the relevant served-app
journeys using disposable data and fake agents, inspecting request, provider
prompt, console and backend logs. Reuse the existing slash-menu browser journey
in `web/tests/agentThreads.browser.test.mjs` at 390px since the UI source does not
change. Report any reachable-host coverage gap; never run tests against a member's live data directory.

Implement and verify in four ordered checkpoints, keeping each committed
checkpoint buildable: shared helpers and focused resolver/prompt tests; Space
first/resumed-turn regressions; project launch owners and their lifecycle and
authority tests; owning specs, served journeys, quality checks and code review.
An active attempt reuses its original selection through correction loops; add a
test that changes Settings during a correction and observes the change only on
the next turn/Retry rather than moving staging inside those loops.

Update current behavior in `docs/specs/providers-and-containment.md`,
`docs/specs/graph-consolidation-and-lessons.md`, and
`docs/specs/conversations-episodes-and-watchers.md` in the same implementation
commit. Run EGM codebase-details on those owning sections, and review the full
diff. Delete this temporary design after its rules live in the owning specs,
before the PR merges. No push or deployment is part of the design step.
