from __future__ import annotations

import uuid

import pytest

from rcp.agents.command_protocol import SpawnArguments, SpawnCommandRequest
from rcp.background import BackgroundAgentTasks
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.runs.auto_research import AutoResearchCommandDispatcher
from rcp.runs.auto_research_effects import auto_research_command_effects
from tests.helpers import wait_for_task
from tests.test_auto_research_effects import (
    CREDENTIAL,
    MAILBOX_ID,
    _blocker_state,
    _effects,
    _setup_auto_research,
    _successful_stream,
    _worker_request,
)


def _fork(store, episode, *, owned=True):
    return store.create_graph_branch(
        project_id=episode.project_id,
        branch_id=str(uuid.uuid4()),
        owner_episode_id=episode.episode_id if owned else None,
        parent_head=GraphHeadRef(target=episode.graph_target, revision=0),
        created_by="root",
        name="child-target",
    )


def _spawn(**arguments):
    return SpawnCommandRequest(
        mailbox_id=MAILBOX_ID,
        request_id=uuid.uuid4().hex,
        credential=CREDENTIAL,
        verb="spawn",
        idempotency_key="child-key",
        arguments=SpawnArguments(instruction_file="worker.md", **arguments),
    )


@pytest.mark.parametrize("project", [False, True])
def test_owned_target_is_pinned_and_same_key_replays_after_rename(tmp_path, project):
    store, episode, root = _setup_auto_research(tmp_path)
    branch = _fork(store, episode)
    target = GraphTargetRef(kind="branch", branch_id=branch.branch_id)
    background = BackgroundAgentTasks(store, _successful_stream)
    base = _effects(store, background)
    effects = auto_research_command_effects(
        store=store,
        background=background,
        validate=base.validate,
        worker_request_factory=lambda context, arguments, instruction, worker_id: _worker_request(
            context, arguments, instruction, worker_id
        ).model_copy(update={"chat_scope": "project" if arguments.project else "node"}),
        graph_state=_blocker_state,
        execution_host="execution.example",
    )
    dispatcher = AutoResearchCommandDispatcher(
        store, effects, command_file_reader=lambda _name, _limit: "Inspect the graph."
    )
    request = _spawn(
        project=project, seat_node_id=None if project else "blk/check", target=branch.name
    )
    before = store.episode_budget_meter(episode.episode_id).invocations_used
    result = dispatcher.dispatch(root.operation_id, request)
    assert result.status == "ok", result
    worker_id = result.result["worker_id"]
    assert isinstance(worker_id, str)
    task = wait_for_task(store, worker_id)
    assert task.graph_target == target
    assert task.kind == ("project_chat" if project else "node_chat")
    command = store.agent_command_by_key(episode.episode_id, "child-key")
    assert command is not None
    assert command.start_payload["resolved_graph_target"] == target.model_dump(mode="json")
    store.rename_branch(episode.project_id, branch.branch_id, "renamed-target")
    replay = dispatcher.dispatch(
        root.operation_id, request.model_copy(update={"request_id": uuid.uuid4().hex})
    )
    assert replay.status == "ok" and replay.result["worker_id"] == worker_id
    assert store.episode_budget_meter(episode.episode_id).invocations_used == before + 1


@pytest.mark.parametrize("refusal", ["foreign", "archived", "atomic_fork"])
def test_refused_target_does_not_admit_or_spend(tmp_path, refusal):
    store, episode, root = _setup_auto_research(tmp_path)
    branch = _fork(store, episode, owned=refusal != "foreign")
    if refusal == "archived":
        store.set_branch_archived(episode.project_id, branch.branch_id, archived=True)
    dispatcher = AutoResearchCommandDispatcher(
        store,
        _effects(store, BackgroundAgentTasks(store, _successful_stream)),
        command_file_reader=lambda _name, _limit: "Inspect the graph.",
    )
    arguments = {"at": "main@0"} if refusal == "atomic_fork" else {"target": branch.name}
    before = store.episode_budget_meter(episode.episode_id)
    result = dispatcher.dispatch(root.operation_id, _spawn(project=True, **arguments))
    assert result.status == "invalid"
    assert result.result["code"] == (
        "fork_atomic_admission_unavailable" if refusal == "atomic_fork" else "child_target_refused"
    )
    assert store.episode_budget_meter(episode.episode_id) == before
    assert store.pending_auto_research_child_admissions(episode.episode_id) == []


def test_root_status_caps_owned_forks_and_counts_target_writers(tmp_path):
    from rcp.agents.command_protocol import StatusArguments
    from rcp.limits import AUTO_RESEARCH_FORK_STATUS_MAX_ROWS
    from tests.test_auto_research_effects import _context

    store, episode, root = _setup_auto_research(tmp_path)
    for _ in range(AUTO_RESEARCH_FORK_STATUS_MAX_ROWS + 1):
        store.create_graph_branch(
            project_id=episode.project_id,
            branch_id=str(uuid.uuid4()),
            owner_episode_id=episode.episode_id,
            parent_head=GraphHeadRef(target=episode.graph_target, revision=0),
            created_by=root.operation_id,
        )
    background = BackgroundAgentTasks(store, _successful_stream)
    effects = auto_research_command_effects(
        store=store,
        background=background,
        validate=_effects(store, background).validate,
        worker_request_factory=_worker_request,
        graph_state=_blocker_state,
        execution_host="execution.example",
        branch_head=lambda target: GraphHeadRef(target=target, revision=0),
    )
    result = effects.status(_context(store, episode, root), StatusArguments()).result["forks"]
    assert isinstance(result, dict)
    assert len(result["rows"]) == AUTO_RESEARCH_FORK_STATUS_MAX_ROWS
    assert result["omitted"] == 1
    assert all(row["workers"] == row["experiments"] == 0 for row in result["rows"])


def test_fork_worker_apply_cannot_write_parent(manifest):
    from rcp.core.authority import WriteAuthority
    from rcp.history import HistoryManager
    from tests.helpers import seated_on_every_project, seed_patch, unchecked_write_authority
    from tests.test_branch_history import _branch_metadata, _branch_patch, _task_authority
    from tests.test_history_forks import fork_metadata

    history = HistoryManager(manifest, write_authority=unchecked_write_authority())
    history.append(seed_patch())
    parent_metadata = _branch_metadata(history)
    parent = history.create_branch(parent_metadata)
    metadata = fork_metadata(history, parent.head_ref()).model_copy(
        update={"episode_id": parent_metadata.episode_id}
    )
    fork = history.create_branch(metadata)
    authority = _task_authority("fork-worker", metadata, fork.graph_target)
    history.project_id = metadata.project_id
    history.write_authority = WriteAuthority()
    history.write_authority.project_membership_check = seated_on_every_project
    history.write_authority.agent_authority_resolver = lambda _project, _operation: authority
    patch = _branch_patch("ev/worker").model_copy(update={"source_operation_id": "fork-worker"})
    parent_head = parent.head_ref()
    with pytest.raises(ValueError):
        parent.append(patch)
    assert parent.head_ref() == parent_head
    fork.append(patch)
    assert "ev/worker" in fork.state().nodes
