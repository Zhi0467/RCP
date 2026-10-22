from __future__ import annotations

import uuid
from unittest.mock import Mock

import pytest

from rcp.agents.command_protocol import SpawnArguments, SpawnCommandRequest
from rcp.background import BackgroundAgentTasks
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.runs.auto_research import AutoResearchCommandDispatcher
from rcp.runs.auto_research_effects import auto_research_command_effects
from tests.helpers import CommandFileMailbox, wait_for_task
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
        store, effects, mailbox=CommandFileMailbox(lambda _name, _limit: "Inspect the graph.")
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


@pytest.mark.parametrize("refusal", ["foreign", "archived"])
def test_refused_target_does_not_admit_or_spend(tmp_path, refusal):
    store, episode, root = _setup_auto_research(tmp_path)
    branch = _fork(store, episode, owned=refusal != "foreign")
    if refusal == "archived":
        store.set_branch_archived(episode.project_id, branch.branch_id, archived=True)
    dispatcher = AutoResearchCommandDispatcher(
        store,
        _effects(store, BackgroundAgentTasks(store, _successful_stream)),
        mailbox=CommandFileMailbox(lambda _name, _limit: "Inspect the graph."),
    )
    arguments = {"target": branch.name}
    before = store.episode_budget_meter(episode.episode_id)
    result = dispatcher.dispatch(root.operation_id, _spawn(project=True, **arguments))
    assert result.status == "invalid"
    assert result.result["code"] == "child_target_refused"
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


def _seed_text(ops=None):
    import json

    setup = {
        "id": "blk/setup",
        "type": "blocker",
        "title": "Setup",
        "description": "Check the prepared branch.",
    }
    return json.dumps(
        {
            "summary": "Prepare the child graph.",
            "ops": ops or [{"op": "create_nodes", "nodes": [setup]}],
        }
    )


def _fork_dispatch(manifest, tmp_path, *, kind="work", seed=True):
    from rcp.agents.command_protocol import EpisodeCommandRequest, ExperimentKickoffArguments
    from tests.helpers import seated_on_every_project
    from tests.test_auto_research_experiments import EXPERIMENT_ID, _setup

    service, store, background, coordinator, episode_id, root_id = _setup(
        manifest, tmp_path, ceiling=8
    )
    episode, root = store.episode(episode_id), store.agent_task(root_id)
    assert episode is not None and root is not None
    service.history.write_authority.project_membership_check = seated_on_every_project

    def worker_request(context, arguments, instruction, worker_id):
        return _worker_request(context, arguments, instruction, worker_id).model_copy(
            update={"chat_scope": "project" if arguments.project else "node"}
        )

    effects = auto_research_command_effects(
        store=store,
        background=background,
        validate=_effects(store, background).validate,
        worker_request_factory=worker_request,
        graph_state=service.history.state,
        execution_host="local",
        experiment_coordinator=coordinator,
    )
    files = {"worker.md": "Inspect the prepared graph.", "seed.json": _seed_text()}
    dispatcher = AutoResearchCommandDispatcher(
        store,
        effects,
        service=service,
        mailbox=CommandFileMailbox(lambda name, _: files[name]),
    )
    at = f"main@{service.history.head_ref().revision}"
    options = {"at": at, **({"seed_file": "seed.json"} if seed else {})}
    request = (
        _spawn(project=True, **options)
        if kind == "work"
        else EpisodeCommandRequest(
            mailbox_id=MAILBOX_ID,
            request_id=uuid.uuid4().hex,
            credential=CREDENTIAL,
            verb="episode",
            idempotency_key="child-key",
            arguments=ExperimentKickoffArguments(
                action="kick_off_experiment",
                node_id=EXPERIMENT_ID,
                at=at,
                seed_file="seed.json" if seed else None,
            ),
        )
    )
    return (
        service,
        store,
        episode,
        root,
        dispatcher,
        request,
        files,
        background,
        coordinator,
        worker_request,
    )


@pytest.mark.parametrize("kind", ["work", "experiment"])
@pytest.mark.parametrize("seed", [False, True])
@pytest.mark.parametrize("lost_reply", [False, True])
def test_fork_admission_replays_one_target_and_seed(
    manifest, tmp_path, monkeypatch, kind, seed, lost_reply
):
    service, store, episode, root, dispatcher, request, files, _, _, _ = _fork_dispatch(
        manifest, tmp_path, kind=kind, seed=seed
    )
    parent_head = service.history.head_ref()
    if lost_reply:
        with monkeypatch.context() as patcher:
            patcher.setattr(dispatcher, "_record_finish", Mock(side_effect=OSError("lost reply")))
            with pytest.raises(OSError):
                dispatcher.dispatch(root.operation_id, request)
        request = request.model_copy(update={"request_id": uuid.uuid4().hex})
    response = dispatcher.dispatch(root.operation_id, request)
    assert response.status == "ok", response.message
    branch_id = response.result["branch_id"]
    assert isinstance(branch_id, str)
    branch = store.graph_branch(episode.project_id, branch_id)
    assert branch is not None and branch.auto_archive and branch.parent_head == parent_head
    assert branch.owner_episode_id == episode.episode_id
    fork = service.history.branch(branch_id)
    assert fork.head_ref().revision == parent_head.revision + int(seed)
    assert ("blk/setup" in fork.state().nodes) == seed
    assert "blk/setup" not in service.history.state().nodes
    meter = store.episode_budget_meter(episode.episode_id)
    allowance = store.auto_research_experiment_allowance(episode.episode_id)
    files["seed.json"] = "changed after admission"
    replay = dispatcher.dispatch(
        root.operation_id, request.model_copy(update={"request_id": uuid.uuid4().hex})
    )
    assert replay.status == "ok" and replay.result["branch_id"] == branch_id
    assert store.episode_budget_meter(episode.episode_id) == meter
    assert store.auto_research_experiment_allowance(episode.episode_id) == allowance
    assert len(fork.load_patches()) == int(seed)


@pytest.mark.parametrize("stopped", [False, True])
@pytest.mark.parametrize("phase", ["publish", "seed", "launch"])
@pytest.mark.parametrize("kind", ["work", "experiment"])
def test_crashed_fork_admission_recovers_exact_fork_and_seed(
    manifest, tmp_path, monkeypatch, phase, kind, stopped
):
    import rcp.runs.auto_research_fork_admission as forks
    from rcp.runs.auto_research_child_reconcile import (
        reconcile_pending_auto_research_child_admissions,
    )

    (
        service,
        store,
        episode,
        root,
        dispatcher,
        request,
        files,
        background,
        coordinator,
        worker_request,
    ) = _fork_dispatch(manifest, tmp_path, kind=kind)

    def crash(*_args, **_kwargs):
        raise OSError("simulated interruption")

    with monkeypatch.context() as patcher:
        if phase == "publish":
            patcher.setattr(forks, "create_graph_fork", crash)
        elif phase == "seed":
            patcher.setattr(forks, "_apply_work_patch", crash)
        else:
            patcher.setattr(dispatcher, "_execute", crash)
        response = dispatcher.dispatch(root.operation_id, request)
    assert response.status == "unavailable", response
    command = store.agent_command_by_key(episode.episode_id, "child-key")
    assert command is not None
    target = GraphTargetRef.model_validate(command.start_payload["resolved_graph_target"])
    files.clear()
    if stopped:
        store.request_episode_stop(episode.episode_id)
    result = reconcile_pending_auto_research_child_admissions(
        store,
        background,
        coordinator,
        worker_request_factory=worker_request,
        seat_node_type=lambda *_: None,
        project_service=lambda _: service,
    )
    assert target.branch_id is not None
    if stopped:
        assert (result.reflected, result.cancelled, result.deferred) == (0, 1, 0), result
        if phase == "publish":
            assert not (service.history.root / "branches" / target.branch_id).exists()
        else:
            assert len(service.history.branch(target.branch_id).load_patches()) == int(
                phase == "launch"
            )
        return
    assert (result.reflected, result.cancelled, result.deferred) == (1, 0, 0), result
    fork = service.history.branch(target.branch_id)
    assert "blk/setup" in fork.state().nodes
    assert len(fork.load_patches()) == 1
    child_id = command.start_payload[
        "planned_worker_id" if kind == "work" else "planned_episode_effect_id"
    ]
    assert isinstance(child_id, str)
    child = store.agent_task(child_id) if kind == "work" else store.episode(child_id)
    assert child is not None and child.graph_target == target


def test_invalid_experiment_limit_does_not_reserve_or_seed_a_fork(manifest, tmp_path):
    _, store, episode, root, dispatcher, request, *_ = _fork_dispatch(
        manifest, tmp_path, kind="experiment"
    )
    before = store.branches_owned_by(episode.project_id, episode.episode_id)
    limit = store.auto_research_experiment_allowance(episode.episode_id).total + 1
    request = request.model_copy(
        update={"arguments": request.arguments.model_copy(update={"invocation_limit": limit})}
    )
    assert dispatcher.dispatch(root.operation_id, request).status == "invalid"
    assert store.branches_owned_by(episode.project_id, episode.episode_id) == before
    assert store.agent_command_by_key(episode.episode_id, "child-key") is None


def test_fork_seed_cannot_change_existing_protected_belief(manifest, tmp_path):
    from tests.helpers import seed_patch

    service, store, episode, root, dispatcher, request, files, _, _, _ = _fork_dispatch(
        manifest, tmp_path
    )
    service.history.append(seed_patch())
    node_id = "hyp/replanning-restores-plasticity"
    original = service.history.state().nodes[node_id]
    request = request.model_copy(
        update={
            "arguments": request.arguments.model_copy(
                update={"at": f"main@{service.history.head_ref().revision}"}
            )
        }
    )
    files["seed.json"] = _seed_text(
        [
            {
                "op": "update_nodes",
                "nodes": [{"id": node_id, "changes": {"statement": "Changed belief"}}],
            }
        ]
    )
    response = dispatcher.dispatch(root.operation_id, request)
    assert response.status == "invalid", response
    command = store.agent_command_by_key(episode.episode_id, "child-key")
    assert command is not None
    target = GraphTargetRef.model_validate(command.start_payload["resolved_graph_target"])
    assert target.branch_id is not None
    fork = service.history.branch(target.branch_id)
    assert fork.state().nodes[node_id] == original
    assert fork.load_patches() == []
    child_id = command.start_payload["planned_worker_id"]
    assert isinstance(child_id, str) and store.auto_research_child_work(child_id) is None


def test_seed_revision_prevents_autoarchive_after_child_settles(manifest, tmp_path):
    import logging

    from rcp.runs.auto_research_forks import archive_settled_auto_research_forks

    service, store, episode, root, dispatcher, request, _, _, _, _ = _fork_dispatch(
        manifest, tmp_path
    )
    response = dispatcher.dispatch(root.operation_id, request)
    assert response.status == "ok", response.message
    worker_id, branch_id = response.result["worker_id"], response.result["branch_id"]
    assert isinstance(worker_id, str) and isinstance(branch_id, str)
    wait_for_task(store, worker_id)
    archive_settled_auto_research_forks(
        store, lambda _: service, logger=logging.getLogger(__name__)
    )
    assert not store.graph_branch(episode.project_id, branch_id).archived
