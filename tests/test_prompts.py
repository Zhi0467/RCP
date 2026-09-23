from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.agents import validate_work_patch
from rcp.agents.experiment_loop_prompt import (
    experiment_loop_continuation_contract,
    experiment_loop_task_contract,
)
from rcp.agents.graph_rules import REPEATED_RULES_NOTE, graph_rules
from rcp.agents.prompts import PromptFactory
from rcp.agents.write_scope import ProjectWriteScope, WritableRepositoryRoot
from rcp.core.models import HUMAN_EDITABLE_NODE_FIELDS, GraphState
from rcp.core.operations import CoverageUpdate, SetCoverageOperation
from rcp.core.transition_models import GraphTargetRef
from rcp.core.validation.experiment_loop import (
    _PINNED_DECISION_FIELDS,
    PINNED_DECISION_BALLOT_FIELDS,
)
from rcp.providers import ProviderSkillReference
from rcp.runs.chat import _chat_context_delta
from rcp.runs.experiment_loop import stage_experiment_loop_context
from rcp.service import RunRequest
from rcp.skill_registry import official_registry
from tests.helpers import seed_patch


@pytest.fixture
def execution_instructions():
    return "test-client launch --scope example --output /stage/launch-receipt.json"


def test_work_compute_handoff_preserves_the_resolved_execution_instructions(execution_instructions):
    contract = _work_contract(
        watch_path="/stage/watch.json", execution_instructions=execution_instructions
    )
    assert execution_instructions in contract


def test_experiment_ballot_fields_match_the_enforcement_allowlist() -> None:
    # Enforcement admits the ballot fields plus the queued status, and nothing
    # else. `selected_option` is the field the whole rule exists to withhold.
    assert {*PINNED_DECISION_BALLOT_FIELDS, "status"} == _PINNED_DECISION_FIELDS
    assert "selected_option" not in _PINNED_DECISION_FIELDS
    # The loop may only restate content the Decision card already carries, so its
    # set cannot name a field outside the editable-content registry.
    assert HUMAN_EDITABLE_NODE_FIELDS["decision"] >= _PINNED_DECISION_FIELDS


def test_launch_prompt_preserves_the_contract_path() -> None:
    contract_path = "/tmp/rcp-run.example/inputs/task-op-initial.md"
    prompt = PromptFactory.launch_prompt(contract_path)

    assert contract_path in prompt


def test_chat_master_context_preserves_context_and_skill_paths() -> None:
    package = official_registry().package("skill", "graph-audit")
    skill_path = "/stage/inputs/skills/skill/graph-audit"
    master = PromptFactory.chat_master_context(
        project_name="Example",
        ontology_path="/state/graph.json#ontology",
        ontology_extensions=True,
        graph_path="/state/graph.json",
        research_path="/state/research.md",
        graph_revision=7,
        focused_node_id="rq/example",
        repositories=[{"alias": "repo-a", "host": "", "path": "/repo-a"}],
        introduction_path="/state/paper/introduction.md",
        patch_path="/stage/workspace/patch.json",
        workspace_path="/stage/workspace",
        output_schema_path="/stage/inputs/chat-patch-schema.json",
        validator_command="python /stage/inputs/validator.py /stage/workspace/patch.json",
        watch_path="/stage/workspace/watch.json",
        skill_pointers=[package.catalog_entry() | {"path": skill_path}],
    )

    assert master.count(skill_path) == 1
    for path in ("/state/graph.json", "/state/research.md", "/state/paper/introduction.md"):
        assert path in master
    # Discuss and Work share one copy; the editing method is labelled as method, not authority.
    assert master.count(graph_rules(edits=True, ontology_extensions=True)) == 1


def test_chat_master_preserves_experiment_watcher_resource_paths_and_host() -> None:
    resource = {
        "control_node_id": "exp/example",
        "episode_id": "episode-1",
        "execution_host": "episode.example",
        "watcher_state_path": "/stage/inputs/exp-example-watchers.json",
        "watch_path": "/stage/workspace/experiment-watch-example.json",
    }
    master = PromptFactory.chat_master_context(
        project_name="Example",
        ontology_path="/state/graph.json#ontology",
        ontology_extensions=False,
        graph_path="/state/graph.json",
        research_path="/state/research.md",
        graph_revision=7,
        focused_node_id="exp/example",
        repositories=[],
        introduction_path=None,
        patch_path="/stage/workspace/patch.json",
        workspace_path="/stage/workspace",
        output_schema_path="/stage/inputs/schema.json",
        validator_command="python3 /stage/inputs/validate.py",
        watch_path="/stage/workspace/watch.json",
        execution_host="chat.example",
        experiment_watcher_resources=[resource],
    )

    assert resource["watcher_state_path"] in master
    assert resource["watch_path"] in master
    assert resource["execution_host"] in master
    assert "/stage/workspace/watch.json" in master


@pytest.mark.parametrize("bootstrap", [False, True])
@pytest.mark.parametrize("mode", ["discuss", "work"])
def test_chat_turn_preserves_human_message_and_input_paths(mode, bootstrap) -> None:
    message = "/evidence-triage  keep  these\nexact bytes"
    master_path = "/stage/inputs/chat-master.md"
    artifact_path = "/stage/workspace/turns/op-2/artifacts"
    build = (
        PromptFactory.discuss_turn_prompt if mode == "discuss" else PromptFactory.work_turn_prompt
    )

    prompt = build(
        artifact_path=artifact_path,
        human_message=message,
        master_context_path=master_path,
        bootstrap_master_context=bootstrap,
        context_delta={"repositories": [{"alias": "repo-b", "path": "/repo-b"}]},
    )

    assert prompt.count(message) == 1
    assert prompt.count(master_path) == 1
    assert artifact_path in prompt
    assert "/repo-b" in prompt


def test_structured_invocation_activates_exact_pointer_without_rewriting_human_message() -> None:
    message = "/graph-audit  keep  spacing\nand punctuation?!"
    graph_audit = {
        "id": "graph-audit",
        "kind": "skill",
        "label": "Graph audit",
        "version": "3.0.0",
        "path": "/stage/inputs/skills/skill/graph-audit",
    }
    for prompt in (
        PromptFactory.discuss_turn_prompt(
            artifact_path="/stage/artifacts",
            human_message=message,
            invoked_skill_pointers=[graph_audit],
        ),
        PromptFactory.work_turn_prompt(
            artifact_path="/stage/artifacts",
            human_message=message,
            invoked_skill_pointers=[graph_audit],
        ),
    ):
        assert prompt.count(message) == 1
        assert graph_audit["path"] in prompt


def test_provider_native_invocation_preserves_metadata_and_native_token() -> None:
    message = "/native-review  keep  these bytes\nand punctuation?!"
    reference = ProviderSkillReference(
        provider="codex",
        machine="laptop",
        provider_version="codex-cli 0.146.1",
        inventory_hash="f" * 64,
        name="native-review",
        label="Native review",
        description="Review using the provider-native checklist.",
        stale=True,
    )
    payload = reference.model_dump(mode="json") | {"native_token": "$native-review"}
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    prompt = PromptFactory.discuss_turn_prompt(
        artifact_path="/stage/artifacts",
        human_message=message,
        invoked_provider_skills=[reference],
    )

    assert prompt.count(message) == 1
    assert serialized in prompt

    resume = PromptFactory.continuation_task_contract(
        original_contract_path="/stage/original.md",
        mode="resume",
        invoked_provider_skills=[reference],
    )
    retry = PromptFactory.continuation_task_contract(
        original_contract_path="/stage/original.md",
        diagnostics_path="/stage/diagnostics.json",
        mode="retry",
        invoked_provider_skills=[reference],
    )
    paper = PromptFactory.paper_coach_task_contract(
        introduction_path="/project/introduction.md",
        graph_path="/project/graph.json",
        research_path="/project/research.md",
        repositories=[],
        human_request_path="/stage/human-request.txt",
        invoked_provider_skills=[reference],
    )
    for contract in (resume, retry, paper):
        assert serialized in contract


def test_graph_contract_preserves_input_paths_watermark_and_validator_command() -> None:
    validator_command = "python /stage/validator.py /stage/workspace/patch.json"
    contract = PromptFactory.graph_task_contract(
        "refresh",
        project_name="Example",
        ontology_path="/state/graph.json#ontology",
        ontology_extensions=True,
        graph_path="/state/graph.json",
        research_path="/state/research.md",
        provider_log_roots={
            "provider-x": ["/provider/logs/provider-x", "/provider/archive/provider-x"]
        },
        ingestion_watermark="2026-07-31T07:00:00-07:00",
        repositories=[{"alias": "repo-a", "host": "", "path": "/repo-a"}],
        patch_path="/stage/workspace/patch.json",
        output_schema_path="/stage/inputs/patch-schema.json",
        validator_command=validator_command,
        human_request_path="/stage/inputs/human-request.txt",
        retry_diagnostics_path="/stage/inputs/retry-diagnostics.json",
    )

    assert "/provider/logs/provider-x" in contract
    assert "2026-07-31T07:00:00-07:00" in contract
    assert "/state/graph.json#ontology" in contract
    assert "/stage/inputs/patch-schema.json" in contract
    assert "/stage/inputs/human-request.txt" in contract
    assert "/stage/inputs/retry-diagnostics.json" in contract
    assert "/stage/workspace/patch.json" in contract
    assert "/provider/archive/provider-x" in contract
    assert validator_command in contract
    assert graph_rules(edits=True, ontology_extensions=True) in contract


def test_work_contract_preserves_inputs_outputs_and_validator_command() -> None:
    validator_command = "python /stage/validate_patch.py --token work-token"
    contract = PromptFactory.work_task_contract(
        project_name="Example",
        ontology_path="/state/graph.json#ontology",
        ontology_extensions=True,
        graph_path="/state/graph.json",
        research_path="/state/research.md",
        focused_node_id="hyp/example",
        repositories=[
            {"alias": "repo-a", "host": "", "path": "/repo-a"},
            {"alias": "repo-b", "host": "gpu.example", "path": "/srv/repo-b"},
        ],
        introduction_path=None,
        human_request_path="/stage/inputs/human-request.txt",
        patch_path="/stage/patch.json",
        artifact_path="/stage/artifacts",
        output_schema_path="/stage/inputs/patch-schema.json",
        validator_command=validator_command,
    )

    assert "/state/graph.json" in contract
    assert "/stage/inputs/human-request.txt" in contract
    assert "/stage/inputs/patch-schema.json" in contract
    assert "/stage/artifacts" in contract
    assert "/srv/repo-b" in contract
    assert "gpu.example" in contract
    assert "/stage/patch.json" in contract
    assert validator_command in contract
    assert graph_rules(edits=True, ontology_extensions=True) in contract


@pytest.mark.asyncio
async def test_experiment_loop_context_fails_closed_without_episode_binding() -> None:
    request = RunRequest(
        patch_kind="experiment_loop",
        control_node_id="exp/example",
        control_revision=1,
    )

    with pytest.raises(ValueError, match="episode invocation binding"):
        await stage_experiment_loop_context(
            object(),  # type: ignore[arg-type]
            request,
            None,
            None,
            None,
            token="missing-binding",
            continuation="fresh",
        )


@pytest.mark.asyncio
async def test_pending_completion_context_names_human_reauthorization(tmp_path) -> None:
    class Store:
        def agent_task(self, operation_id):
            assert operation_id == "operation"
            return SimpleNamespace(project_id="project")

        def watchers(self, project_id):
            assert project_id == "project"
            return []

    execution = SimpleNamespace(operation_id="operation", store=Store())
    service = SimpleNamespace(history=SimpleNamespace(state=lambda: GraphState()))
    request = RunRequest(
        trigger="experiment_run",
        patch_kind="experiment_loop",
        control_node_id="exp/example",
        control_revision=2,
        control_episode_id="d91bb1b3-a480-4dbf-b5f0-4bd62bf4f779",
        control_invocation=1,
        control_invocation_ceiling=3,
        watcher_ids=["watcher-from-old-episode"],
    )

    control_path, _ = await stage_experiment_loop_context(
        service,
        request,
        execution,
        tmp_path / "stage",
        None,
        token="reauthorized",
        continuation="fresh",
    )

    control = json.loads(Path(control_path).read_text(encoding="utf-8"))
    assert control["phase"] == "human_reauthorization"
    assert control["invocation"] == 1
    assert control["delivered_watcher_ids"] == ["watcher-from-old-episode"]


@pytest.mark.asyncio
async def test_watcher_wake_context_keeps_every_delivered_group_member(tmp_path) -> None:
    def watcher(
        watcher_id: str,
        *,
        status: str,
        episode_id: str,
        stopped_by: str | None = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            watcher_id=watcher_id,
            origin_operation_id="older-operation",
            graph_target=GraphTargetRef(),
            execution_host="",
            check_command="test -f complete",
            cancel_command=None,
            cancel_requested_by=None,
            cancel_requested_at=None,
            cancel_error=None,
            log_path=f"/tmp/{watcher_id}.log",
            cwd="/tmp",
            status=status,
            created_at="2026-08-07T00:00:00+00:00",
            last_checked_at="2026-08-07T00:00:00+00:00",
            last_exit_code=0 if status == "completed" else None,
            last_error=None,
            completed_at="2026-08-07T00:00:00+00:00" if status == "completed" else None,
            next_check_at=None,
            consecutive_error_count=0,
            group_id="group/replicas",
            group_label="replicas",
            notified=True,
            notification_operation_id="wake-operation",
            stopped_by=stopped_by,
            stop_reason="superseded replica" if stopped_by else None,
            stopped_at="2026-08-07T00:00:00+00:00" if stopped_by else None,
            stop_operation_id="older-operation" if stopped_by else None,
            continuation=SimpleNamespace(
                patch_kind="experiment_loop",
                control_node_id="exp/example",
                control_episode_id=episode_id,
                control_invocation=1,
                control_invocation_ceiling=3,
                control_revision=2,
                control_decision_bundle=[],
            ),
        )

    delivered = watcher("watcher/completed", status="completed", episode_id="old-episode")
    agent_stopped = watcher(
        "watcher/agent-stopped",
        status="stopped",
        episode_id="even-older-episode",
        stopped_by="agent",
    )

    class Store:
        def agent_task(self, operation_id):
            assert operation_id == "wake-operation"
            return SimpleNamespace(project_id="project", graph_target=GraphTargetRef())

        def watchers(self, project_id):
            assert project_id == "project"
            return [delivered, agent_stopped]

    execution = SimpleNamespace(operation_id="wake-operation", store=Store())
    service = SimpleNamespace(history=SimpleNamespace(state=lambda: GraphState()))
    request = RunRequest(
        trigger="watcher",
        patch_kind="experiment_loop",
        control_node_id="exp/example",
        control_revision=2,
        control_episode_id="new-episode",
        control_invocation=2,
        control_invocation_ceiling=3,
        watcher_ids=["watcher/completed"],
    )

    control_path, watcher_state_path = await stage_experiment_loop_context(
        service,
        request,
        execution,
        tmp_path / "stage",
        None,
        token="delivered-group",
        continuation="fresh",
    )

    control = json.loads(Path(control_path).read_text(encoding="utf-8"))
    assert control["delivered_watcher_groups"] == [
        {
            "group_id": "group/replicas",
            "label": "replicas",
            "members": json.loads(Path(watcher_state_path).read_text(encoding="utf-8")),
        }
    ]
    assert {
        member["watcher_id"] for member in control["delivered_watcher_groups"][0]["members"]
    } == {"watcher/completed", "watcher/agent-stopped"}


def test_experiment_contract_preserves_control_paths_and_resolved_commands(
    execution_instructions,
) -> None:
    validator_command = "python /stage/validator.py /stage/patch.json"
    contract = experiment_loop_task_contract(
        execution_instructions=execution_instructions,
        project_name="Example",
        ontology_path="/state/graph.json#ontology",
        ontology_extensions=True,
        graph_path="/state/graph.json",
        research_path="/state/research.md",
        focused_experiment_id="exp/example",
        repositories=[{"alias": "repo-a", "host": "gpu", "path": "/repo-a"}],
        introduction_path=None,
        human_request_path="/stage/inputs/human-request.txt",
        loop_control_path="/stage/inputs/experiment-control.json",
        watcher_state_path="/stage/inputs/experiment-watchers.json",
        patch_path="/stage/patch.json",
        artifact_path="/stage/artifacts",
        output_schema_path="/stage/inputs/patch-schema.json",
        watch_path="/stage/watch.json",
        validator_command=validator_command,
    )

    assert "/stage/inputs/experiment-control.json" in contract
    assert "/stage/inputs/experiment-watchers.json" in contract
    assert execution_instructions in contract
    assert validator_command in contract
    assert graph_rules(edits=True, ontology_extensions=True) in contract
    # A fresh contract states the rules once; only continuations call them a repeat.
    assert REPEATED_RULES_NOTE not in contract


def test_provider_switch_recovery_preserves_diagnostics_path(
    execution_instructions,
) -> None:
    contract = experiment_loop_task_contract(
        execution_instructions=execution_instructions,
        project_name="Example",
        ontology_path="/state/graph.json#ontology",
        ontology_extensions=False,
        graph_path="/state/graph.json",
        research_path="/state/research.md",
        focused_experiment_id="exp/example",
        repositories=[{"alias": "repo-a", "host": "gpu", "path": "/repo-a"}],
        introduction_path=None,
        human_request_path="/stage/inputs/human-request.txt",
        loop_control_path="/stage/inputs/experiment-control.json",
        watcher_state_path="/stage/inputs/experiment-watchers.json",
        patch_path="/stage/patch.json",
        artifact_path="/stage/artifacts",
        output_schema_path="/stage/inputs/patch-schema.json",
        watch_path="/stage/watch.json",
        validator_command="python /stage/validator.py /stage/patch.json",
        recovery_diagnostics_path="/stage/inputs/provider-switch-diagnostics.json",
    )

    assert "/stage/inputs/provider-switch-diagnostics.json" in contract


def test_discuss_contract_preserves_artifact_path() -> None:
    contract = PromptFactory.discuss_task_contract(
        project_name="Example",
        ontology_path="/state/graph.json#ontology",
        ontology_extensions=True,
        graph_path="/state/graph.json",
        research_path="/state/research.md",
        focused_node_id=None,
        repositories=[],
        introduction_path=None,
        human_request_path="/stage/inputs/human-request.txt",
        artifact_path="/stage/artifacts",
    )

    assert "/stage/artifacts" in contract
    assert graph_rules(edits=False, ontology_extensions=True) in contract
    assert "Editing the graph:" not in contract


def test_paper_and_continuation_contracts_only_point_to_dynamic_content() -> None:
    invoked = {
        "id": "evidence-triage",
        "kind": "skill",
        "label": "Evidence triage",
        "version": "3.0.0",
        "path": "/stage/skills/evidence-triage",
    }
    paper = PromptFactory.paper_coach_task_contract(
        introduction_path="/state/paper/introduction.md",
        graph_path="/state/graph.json",
        research_path="/state/research.md",
        repositories=[{"alias": "repo-a", "host": "", "path": "/repo-a"}],
        human_request_path="/stage/inputs/human-request.txt",
        retry_diagnostics_path="/stage/inputs/retry.json",
        invoked_skill_pointers=[invoked],
    )
    assert invoked["path"] in paper
    assert graph_rules(edits=False, ontology_extensions=False) in paper
    correction = PromptFactory.continuation_task_contract(
        original_contract_path="/stage/inputs/task-initial.md",
        mode="patch_correction",
        patch_path="/stage/patch.json",
        diagnostics_path="/stage/inputs/correction.json",
        validator_command="python /stage/validator.py /stage/patch.json",
        ontology_extensions=False,
    )
    watcher = PromptFactory.continuation_task_contract(
        original_contract_path="/stage/inputs/task-initial.md",
        mode="watch_correction",
        diagnostics_path="/stage/inputs/watch-correction.json",
        watch_path="/stage/watch.json",
    )

    assert "/state/paper/introduction.md" in paper
    assert "/stage/inputs/human-request.txt" in paper
    assert "/stage/inputs/retry.json" in paper
    assert "/stage/inputs/correction.json" in correction
    assert "/stage/inputs/task-initial.md" in correction
    assert "/stage/patch.json" in correction
    assert "/stage/watch.json" in watcher
    assert "/stage/inputs/watch-correction.json" in watcher


def test_work_patch_correction_preserves_paths_and_validator_command() -> None:
    validator_command = "python /stage/validate_patch.py --token correction-token"
    correction = PromptFactory.continuation_task_contract(
        original_contract_path="/stage/inputs/task-initial.md",
        mode="work_patch_correction",
        patch_path="/stage/patch.json",
        diagnostics_path="/stage/inputs/correction.json",
        validator_command=validator_command,
        ontology_extensions=True,
    )

    assert validator_command in correction
    assert "/stage/patch.json" in correction
    assert "/stage/inputs/correction.json" in correction
    # A correction repeats the current rules; it does not claim to replace a same-version copy.
    assert REPEATED_RULES_NOTE in correction
    assert graph_rules(edits=True, ontology_extensions=True) in correction
    with pytest.raises(ValueError, match="graph rules"):
        PromptFactory.continuation_task_contract(
            original_contract_path="/stage/inputs/task-initial.md",
            mode="work_patch_correction",
            patch_path="/stage/patch.json",
            diagnostics_path="/stage/inputs/correction.json",
            validator_command=validator_command,
        )


def test_experiment_retry_preserves_fresh_control_path() -> None:
    retry = experiment_loop_continuation_contract(
        original_contract_path="/stage/inputs/task-initial.md",
        mode="retry",
        patch_path="/stage/patch.json",
        watch_path="/stage/watch.json",
        diagnostics_path="/stage/inputs/retry.json",
        output_schema_path="/stage/inputs/patch-schema.json",
        validator_command="python /stage/validator.py /stage/patch.json",
        loop_control_path="/stage/inputs/experiment-control-retry.json",
    )

    assert "/stage/inputs/experiment-control-retry.json" in retry
    assert REPEATED_RULES_NOTE in retry


def test_retry_contract_requires_diagnostics_and_preserves_contract_paths() -> None:
    retry = PromptFactory.continuation_task_contract(
        original_contract_path="/prior/inputs/task-initial.md",
        current_contract_path="/current/inputs/task-initial.md",
        mode="retry",
        patch_path="/current/patch.json",
        diagnostics_path="/current/inputs/retry-diagnostics.json",
    )

    with pytest.raises(ValueError, match="exact diagnostics_path"):
        PromptFactory.continuation_task_contract(
            original_contract_path="/prior/inputs/task-initial.md",
            current_contract_path="/current/inputs/task-initial.md",
            mode="retry",
        )

    assert "/prior/inputs/task-initial.md" in retry
    assert "/current/inputs/task-initial.md" in retry
    assert "/current/patch.json" in retry
    assert "/current/inputs/retry-diagnostics.json" in retry


def test_retry_handoff_contract_preserves_paths() -> None:
    contract = PromptFactory.retry_handoff_task_contract(
        kind="seed",
        handoff_path="/stage/inputs/task-retry-handoff.json",
        original_contract_path="/prior/inputs/task-initial.md",
        patch_path="/stage/patch.json",
        validator_command="python /stage/validator.py /stage/patch.json",
        ontology_extensions=False,
    )

    assert "/stage/inputs/task-retry-handoff.json" in contract
    assert "/prior/inputs/task-initial.md" in contract
    assert "/stage/patch.json" in contract
    # A handoff starts a fresh provider session, so it states the current rules outright.
    assert graph_rules(edits=True, ontology_extensions=False) in contract
    assert REPEATED_RULES_NOTE not in contract


def test_work_patch_legality_reuses_the_non_ingest_boundary_with_work_wording() -> None:
    cursor_patch = seed_patch().model_copy(
        update={"kind": "work", "processed_cursors": {"session": "record"}}
    )
    coverage_patch = seed_patch().model_copy(
        update={
            "kind": "work",
            "ops": [SetCoverageOperation(op="set_coverage", coverage=CoverageUpdate())],
        }
    )

    with pytest.raises(ValueError, match="A Work patch must not claim processed_cursors"):
        validate_work_patch(cursor_patch)
    with pytest.raises(ValueError, match="A Work patch must not set coverage"):
        validate_work_patch(coverage_patch)


def _work_write_scope() -> ProjectWriteScope:
    return ProjectWriteScope.create(
        project_id="project-1",
        execution_machine="laptop",
        execution_host="",
        capability="work_auto",
        stage_root="/stage",
        workspace_root="/stage",
        repositories=[WritableRepositoryRoot(alias="repo-a", machine="laptop", path="/repo-a")],
        protected_write_paths=["/repo-a/.research", "/state/.research"],
    )


def _work_contract(**overrides: object) -> str:
    arguments: dict[str, object] = {
        "project_name": "Example",
        "ontology_path": "/state/graph.json#ontology",
        "ontology_extensions": True,
        "graph_path": "/state/graph.json",
        "research_path": "/state/research.md",
        "focused_node_id": "hyp/example",
        "repositories": [
            {"alias": "repo-a", "host": "", "path": "/repo-a"},
            {"alias": "repo-b", "host": "gpu.example", "path": "/srv/repo-b"},
        ],
        "introduction_path": None,
        "human_request_path": "/stage/inputs/human-request.txt",
        "patch_path": "/stage/patch.json",
        "artifact_path": "/stage/artifacts",
        "output_schema_path": "/stage/inputs/patch-schema.json",
        "validator_command": "python /stage/validate_patch.py --token work-token",
    }
    arguments.update(overrides)
    return PromptFactory.work_task_contract(**arguments)  # type: ignore[arg-type]


def test_work_launch_contract_preserves_resolved_scope_paths() -> None:
    scope = _work_write_scope()
    contract = _work_contract(write_scope=scope)

    assert scope.workspace_root in contract
    for repository in scope.repositories:
        assert repository.path in contract
    for path in scope.protected_write_paths:
        assert path in contract


def test_discuss_turn_rejects_a_work_write_scope() -> None:
    with pytest.raises(ValueError, match="only to a Work turn"):
        PromptFactory._chat_turn_prompt(
            marker="Discuss",
            artifact_path="/stage/turns/t1/artifacts",
            human_message="What do we know?",
            master_context_path=None,
            context_delta=None,
            invoked_skill_pointers=None,
            invoked_provider_skills=None,
            attachments=None,
            write_scope=_work_write_scope(),
        )


def test_compute_resources_preserve_selected_connection_metadata() -> None:
    master = PromptFactory.chat_master_context(
        project_name="Example",
        ontology_path="/state/graph.json#ontology",
        ontology_extensions=True,
        graph_path="/state/graph.json",
        research_path="/state/research.md",
        graph_revision=7,
        focused_node_id=None,
        repositories=[],
        introduction_path=None,
        patch_path="/stage/workspace/patch.json",
        workspace_path="/stage/workspace",
        output_schema_path="/stage/inputs/schema.json",
        validator_command="python3 /stage/inputs/validate.py",
        compute_connections=[
            {
                "id": "gpu",
                "name": "GPU VM",
                "kind": "ssh",
                "ssh_target": "alice@gpu.example",
                "access_hint": "Use /scratch/shared",
            }
        ],
    )

    assert "GPU VM" in master
    assert "alice@gpu.example" in master
    assert "Use /scratch/shared" in master


def test_compute_context_delta_tracks_added_removed_and_updated_connections() -> None:
    previous = {
        "compute": {
            "active": [
                {"id": "local", "name": "Current machine", "kind": "local"},
                {
                    "id": "old",
                    "name": "Old VM",
                    "kind": "ssh",
                    "ssh_target": "alice@old.example",
                },
            ]
        }
    }
    current = {
        "compute": {
            "active": [
                {"id": "local", "name": "Current machine", "kind": "local"},
                {
                    "id": "gpu",
                    "name": "GPU VM",
                    "kind": "ssh",
                    "ssh_target": "alice@gpu.example",
                    "access_hint": "Use /scratch/shared",
                },
            ]
        }
    }

    delta = _chat_context_delta(previous, current)
    assert delta == {
        "compute": {
            "added": [
                {
                    "id": "gpu",
                    "name": "GPU VM",
                    "kind": "ssh",
                    "ssh_target": "alice@gpu.example",
                    "access_hint": "Use /scratch/shared",
                }
            ],
            "removed": ["Old VM"],
            "updated": [],
        }
    }

    updated = {
        "compute": {
            "active": [
                {
                    "id": "gpu",
                    "name": "GPU Accelerator",
                    "kind": "ssh",
                    "ssh_target": "alice@gpu.example",
                    "access_hint": "Use /scratch/new",
                }
            ]
        }
    }
    assert _chat_context_delta(current, updated) == {
        "compute": {
            "added": [],
            "removed": ["Current machine"],
            "updated": [
                {
                    "id": "gpu",
                    "name": "GPU Accelerator",
                    "kind": "ssh",
                    "ssh_target": "alice@gpu.example",
                    "access_hint": "Use /scratch/new",
                }
            ],
        }
    }
    assert _chat_context_delta(updated, updated) is None


def test_watch_correction_preserves_supplied_diagnostics() -> None:
    diagnostic = "Observer check failed with exit code 17 at /jobs/run-1/status"
    contract = PromptFactory.continuation_task_contract(
        original_contract_path="/inputs/original.md",
        mode="watch_correction",
        watch_path="/stage/watch.json",
        diagnostics_path="/inputs/diagnostics.json",
        watcher_diagnostic=diagnostic,
    )

    assert diagnostic in contract


@pytest.mark.parametrize("mode", ["resume", "retry", "watch_correction"])
def test_work_continuation_preserves_execution_instructions_only_for_operational_modes(
    mode, execution_instructions
):
    contract = PromptFactory.continuation_task_contract(
        original_contract_path="/old/task.md",
        mode=mode,
        watch_path="/stage/watch.json",
        diagnostics_path="/stage/diagnostics.json",
        execution_instructions=execution_instructions,
    )
    if mode != "watch_correction":
        assert execution_instructions in contract
    else:
        assert execution_instructions not in contract


@pytest.mark.parametrize("mode", ["resume", "retry"])
def test_experiment_continuation_preserves_current_paths_and_execution_instructions(
    mode, execution_instructions
):
    contract = experiment_loop_continuation_contract(
        original_contract_path="/old/task.md",
        mode=mode,
        loop_control_path="/stage/control.json",
        patch_path="/stage/patch.json",
        watch_path="/stage/watch.json",
        output_schema_path="/stage/schema.json",
        validator_command="test-client validate",
        execution_instructions=execution_instructions,
        diagnostics_path="/stage/diagnostics.json",
        graph_path="/stage/current/graph.json",
        research_path="/stage/current/research.md",
        artifact_path="/stage/turn-2/artifacts",
        write_scope=_work_write_scope(),
    )
    assert execution_instructions in contract
    assert "/stage/current/graph.json" in contract
    assert "/stage/current/research.md" in contract
    assert "/stage/turn-2/artifacts" in contract
    assert "/repo-a/.research" in contract
