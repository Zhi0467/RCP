from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import cast

import pytest

from rcp.api import episode_routes
from rcp.core.models import EpisodeIsolation, EpisodeMergeAttempt, GraphBranchMetadata
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.history.branches import BranchHistoryManager
from rcp.service import ProjectService
from rcp.storage import EpisodeRecord
from rcp.transport import StateUnavailable
from tests.helpers import authorized_human, create_named_app, signed_in_client

from .test_branch_merge_api import (
    _admit_held_merge_task,
    _create_branch_harness,
    _current_receipt,
)
from .test_project_membership import _create_project, _team_app


def test_graph_refs_includes_main_without_episodes(manifest, tmp_path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    response = signed_in_client(app).get(f"/api/projects/{project_id}/graph-refs")
    assert response.status_code == 200, response.text
    [main] = response.json()
    assert main == {
        "kind": "main",
        "name": "main",
        "running": False,
        "needs_you": False,
        "created_at": None,
        "branch_id": None,
        "episode_id": None,
        "current_episode_id": None,
        "head": app.state.catalog.open(project_id).history.head_ref().model_dump(mode="json"),
        "base_head": None,
        "merge_eligible": False,
        "merge_blocked_reason": None,
        "merge_state": None,
        "latest_successful_merge": None,
        "active_merge_task_id": None,
        "merge_diagnostic": None,
        "archived": False,
    }


def test_graph_refs_lists_unique_branches_past_episode_window_and_archive_filters(
    manifest, tmp_path
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    store = app.state.background_tasks.store
    history = app.state.catalog.open(project_id).history
    authorizer = authorized_human(store)
    base_head = history.head_ref()
    now = datetime.fromisoformat(store.now())
    roots = []
    for offset in range(52):
        episode_id = str(uuid.uuid4())
        created_at = (now - timedelta(minutes=offset)).isoformat()
        target = GraphTargetRef(kind="branch", branch_id=episode_id)
        episode = store.create_episode(
            EpisodeRecord(
                episode_id=episode_id,
                project_id=project_id,
                mode="experiment_loop",
                control_node_id=f"exp/{episode_id}",
                graph_target=target,
                graph_base_head=base_head,
                status="queued",
                invocation_ceiling=1,
                authorized_by=authorizer,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        history.create_branch(
            GraphBranchMetadata(
                kind=episode.mode,
                branch_id=episode_id,
                episode_id=episode_id,
                project_id=project_id,
                base_head=base_head,
                head=GraphHeadRef(
                    target=target,
                    revision=base_head.revision,
                    transition_id=base_head.transition_id,
                ),
                authorized_by=authorizer,
            )
        )
        roots.append(episode)
    archived = roots[-1]
    store.create_episode_isolation(
        project_id,
        EpisodeIsolation(owner_episode_id=archived.episode_id, graph_branch_id=archived.episode_id),
    )
    store.set_episode_isolation_status(
        project_id, archived.episode_id, expected_status="creating", status="ready"
    )
    attempt = EpisodeMergeAttempt(attempt_id="archive", authorized_by=authorizer)
    store.reserve_episode_merge(project_id, archived.episode_id, attempt)
    store.finish_episode_merge(
        project_id,
        archived.episode_id,
        expected_attempt_id=attempt.attempt_id,
        attempt=attempt.model_copy(update={"phase": "done"}),
        graph_archived=True,
    )
    # Episode archives are a separate visibility choice and must not hide a ref either.
    store.set_episode_archived(project_id, roots[-2].episode_id, authorizer.user_id, archived=True)
    root = roots[0]
    store.end_episode_without_report(root.episode_id, ending="failed", diagnostic="Ended turn")
    continuation = store.create_episode(
        root.model_copy(
            update={
                "episode_id": str(uuid.uuid4()),
                "continues_episode_id": root.episode_id,
                "isolation_owner_episode_id": root.episode_id,
                "created_at": (now + timedelta(minutes=1)).isoformat(),
            }
        )
    )
    assert archived.episode_id not in {item.episode_id for item in store.episodes(project_id)}

    response = signed_in_client(app).get(f"/api/projects/{project_id}/graph-refs")
    assert response.status_code == 200, response.text
    main, *branches = response.json()
    assert main["kind"] == "main"
    assert len(branches) == len(roots)
    assert {item["branch_id"] for item in branches} == {item.episode_id for item in roots}
    by_id = {item["branch_id"]: item for item in branches}
    assert by_id[archived.episode_id]["archived"] is True
    assert by_id[roots[-2].episode_id]["archived"] is False
    assert branches[0]["branch_id"] == root.episode_id
    assert branches[0]["episode_id"] == root.episode_id
    assert branches[0]["current_episode_id"] == continuation.episode_id
    assert branches[0]["base_head"] == base_head.model_dump(mode="json")
    assert branches[0]["head"]["target"] == root.graph_target.model_dump(mode="json")
    assert branches[0]["merge_state"] == "unmerged"


def test_graph_refs_carries_live_and_completed_merge_projection(manifest, tmp_path, monkeypatch):
    harness = _create_branch_harness(manifest, tmp_path, change="evidence")
    path = f"/api/projects/{harness.project_id}/graph-refs"
    task = _admit_held_merge_task(harness, monkeypatch)
    response = harness.client.get(path)
    assert response.status_code == 200, response.text
    _, branch = response.json()
    assert branch["running"] is True
    assert branch["merge_state"] == "running"
    assert branch["active_merge_task_id"] == task.operation_id
    assert branch["merge_eligible"] is False

    receipt = cast(BranchHistoryManager, harness.branch).write_merge_receipt(
        _current_receipt(harness, task_id=task.operation_id)
    )
    harness.store.complete_agent_task(task.operation_id, applied_revision=None, result={})
    response = harness.client.get(path)
    assert response.status_code == 200, response.text
    _, branch = response.json()
    assert branch["running"] is False
    assert branch["merge_state"] == "merged"
    assert branch["latest_successful_merge"] == receipt.model_dump(mode="json")
    assert branch["active_merge_task_id"] is None
    assert branch["merge_eligible"] is False


def test_graph_refs_requires_project_membership(tmp_path) -> None:
    _app, client, _store, people, acting = _team_app(tmp_path, members=2)
    member, outsider = people
    project_id = _create_project(client, tmp_path / "repo", seat_member=member.user_id)
    assert client.get(f"/api/projects/{project_id}/graph-refs").status_code == 200
    acting[0] = outsider.user_id
    assert client.get(f"/api/projects/{project_id}/graph-refs").status_code == 404


def test_graph_refs_names_a_branch_by_its_owner(manifest, tmp_path) -> None:
    harness = _create_branch_harness(manifest, tmp_path, change="evidence")
    response = harness.client.get(f"/api/projects/{harness.project_id}/graph-refs")
    assert response.status_code == 200, response.text
    _, branch = response.json()
    assert branch["mode"] == "auto_research"
    assert (
        branch["name"] == harness.store.branch_names(harness.project_id)[harness.episode.episode_id]
    )
    assert branch["created_at"] == harness.episode.created_at
    assert "title" not in branch


def test_rename_archive_and_unarchive_preserve_branch_history(manifest, tmp_path) -> None:
    harness = _create_branch_harness(manifest, tmp_path, change="evidence")
    branch_id = harness.episode.episode_id
    path = f"/api/projects/{harness.project_id}/graph-refs/{branch_id}"
    history = cast(BranchHistoryManager, harness.branch)
    before = history.load_patches()
    # Older branch roots need not have any isolation state.
    with harness.store.connection() as connection:
        connection.execute(
            "DELETE FROM episode_isolation_states WHERE owner_episode_id = ?", (branch_id,)
        )
    response = harness.client.put(path + "/name", json={"name": "new-label"})
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "new-label"
    for archived in (True, False):
        response = harness.client.put(path + "/archived", json={"archived": archived})
        assert response.status_code == 200, response.text
        assert response.json()["archived"] is archived
        assert response.json()["name"] == "new-label"
        saved = harness.store.episode_isolation_state(harness.project_id, branch_id)
        assert saved is not None and saved.graph_archived is archived
        if archived:
            # A subsequent Merge can establish the legacy binding without erasing Archive.
            harness.store.create_episode_isolation(
                harness.project_id,
                EpisodeIsolation(owner_episode_id=branch_id, graph_branch_id=branch_id),
            )
            saved = harness.store.episode_isolation_state(harness.project_id, branch_id)
            assert saved is not None and saved.graph_archived is True
    assert history.load_patches() == before


def test_branch_labels_respect_the_transfer_fence_and_leave_with_the_project(
    manifest, tmp_path
) -> None:
    harness = _create_branch_harness(manifest, tmp_path, change="none")
    store, project_id = harness.store, harness.project_id
    path = f"/api/projects/{project_id}/graph-refs/{harness.episode.episode_id}"
    assert harness.client.put(path + "/name", json={"name": "kept-name"}).status_code == 200
    with store.connection() as connection:
        connection.execute(
            """
            INSERT INTO project_transfer_requests (
                request_id, side, phase, project_id, source_space_id,
                target_space_id, record_json, revision, created_at, updated_at
            ) VALUES ('transfer', 'source', 'source_fenced', ?, ?, 'target', '{}', 1, ?, ?)
            """,
            (project_id, store.space_id, store.now(), store.now()),
        )
    assert harness.client.put(path + "/name", json={"name": "late-name"}).status_code == 409
    assert harness.client.put(path + "/archived", json={"archived": True}).status_code == 409
    assert store.branch_names(project_id)[harness.episode.episode_id] == "kept-name"
    with store.connection() as connection:
        connection.execute("DELETE FROM project_transfer_requests")
    store.delete_project_records(project_id)
    with store.connection() as connection:
        for table in ("branch_names", "branch_name_reservations"):
            assert not connection.execute(
                f"SELECT 1 FROM {table} WHERE project_id = ?", (project_id,)
            ).fetchall()


def _unreachable_ref_read(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("the branch read ran before name validation")


@pytest.mark.parametrize("name", ["", "main", "A-name", "a", "-name", "name-", "a" * 33])
def test_rename_branch_invalid_names(manifest, tmp_path, monkeypatch, name) -> None:
    harness = _create_branch_harness(manifest, tmp_path, change="none")
    # An invalid name is refused before the (possibly remote) branch read.
    monkeypatch.setattr(episode_routes, "_branch_ref_for_http", _unreachable_ref_read)
    path = f"/api/projects/{harness.project_id}/graph-refs/{harness.episode.episode_id}/name"
    response = harness.client.put(path, json={"name": name})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "branch_name_invalid"


def test_branch_routes_reject_unknown_main_and_taken_name(manifest, tmp_path) -> None:
    harness = _create_branch_harness(manifest, tmp_path, change="none")
    path = f"/api/projects/{harness.project_id}/graph-refs"
    for branch_id, expected in ((str(uuid.uuid4()), 404), ("main", 422)):
        assert (
            harness.client.put(f"{path}/{branch_id}/name", json={"name": "valid-name"}).status_code
            == expected
        )
        assert (
            harness.client.put(f"{path}/{branch_id}/archived", json={"archived": True}).status_code
            == expected
        )
    branch_id = harness.episode.episode_id
    second_id = str(uuid.uuid4())
    now = harness.store.now()
    harness.store.create_episode(
        EpisodeRecord(
            episode_id=second_id,
            project_id=harness.project_id,
            mode="auto_research",
            graph_target=GraphTargetRef(kind="branch", branch_id=second_id),
            graph_base_head=harness.episode.graph_base_head,
            status="queued",
            invocation_ceiling=1,
            authorized_by=harness.episode.authorized_by,
            created_at=now,
            updated_at=now,
        )
    )
    second_name = harness.store.branch_names(harness.project_id)[second_id]
    harness.store.set_branch_archived(harness.project_id, second_id, archived=True)
    response = harness.client.put(f"{path}/{branch_id}/name", json={"name": second_name})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "branch_name_taken"
    # A branch may take back a name it held before.
    original = harness.store.branch_names(harness.project_id)[branch_id]
    for name in ("interim-name", original):
        response = harness.client.put(f"{path}/{branch_id}/name", json={"name": name})
        assert response.status_code == 200, response.text


def test_branch_mutations_require_project_membership(tmp_path) -> None:
    _app, client, _store, people, acting = _team_app(tmp_path, members=2)
    member, outsider = people
    project_id = _create_project(client, tmp_path / "repo", seat_member=member.user_id)
    acting[0] = outsider.user_id
    path = f"/api/projects/{project_id}/graph-refs/{uuid.uuid4()}"
    assert client.put(path + "/name", json={"name": "valid-name"}).status_code == 404
    assert client.put(path + "/archived", json={"archived": True}).status_code == 404
    assert client.post(path + "/merge", json={"request_id": str(uuid.uuid4())}).status_code == 404
    assert (
        client.post(
            f"/api/projects/{project_id}/graph-refs",
            json={"request_id": str(uuid.uuid4()), "at": "main@0"},
        ).status_code
        == 404
    )


def test_graph_refs_attention_tracks_branch_graph_and_pending_questions(manifest, tmp_path) -> None:
    from rcp.storage.question_models import QuestionOrigin

    harness = _create_branch_harness(manifest, tmp_path, change="status")
    path = f"/api/projects/{harness.project_id}/graph-refs"
    # The main blocker is resolved only on this branch; main attention must not leak.
    assert harness.client.get(path).json()[1]["needs_you"] is False
    question = harness.store.create_or_get_question(
        origin=QuestionOrigin(
            owner_kind="chat",
            project_id=harness.project_id,
            owner_id="chat",
            operation_id=harness.root.operation_id,
            provider="codex",
            native_session_id="session",
            stage_root="/stage",
            capability="discuss",
            graph_target=harness.episode.graph_target,
        ),
        key="pick",
        question="Which result?",
    )
    assert harness.client.get(path).json()[1]["needs_you"] is True
    harness.store.answer_question(
        question.question_id, answer="First", resolved_by=authorized_human(harness.store)
    )
    assert harness.client.get(path).json()[1]["needs_you"] is False


def test_graph_refs_open_blocker_needs_you(manifest, tmp_path) -> None:
    harness = _create_branch_harness(manifest, tmp_path, change="evidence")
    response = harness.client.get(f"/api/projects/{harness.project_id}/graph-refs")
    assert response.status_code == 200, response.text
    assert response.json()[1]["needs_you"] is True


def test_graph_refs_decision_uses_inbox_attention(manifest, tmp_path) -> None:
    from rcp.core.models import Patch

    harness = _create_branch_harness(manifest, tmp_path, change="status")
    history = cast(BranchHistoryManager, harness.branch)
    state = history.current_materialization().state
    history.append(
        Patch.model_validate(
            {
                "kind": "work",
                "author": "agent",
                "producer": "agent",
                "summary": "Ready decision",
                "run_truth_scope": ["repo-a"],
                "repositories_read": ["repo-a"],
                "source_operation_id": harness.root.operation_id,
                "ops": [
                    {
                        "op": "create_nodes",
                        "nodes": [
                            {
                                "id": "dec/pick",
                                "type": "decision",
                                "title": "Pick a direction",
                                "question": "Which direction?",
                                "options": ["First", "Second"],
                                "status": "ready",
                            }
                        ],
                    }
                ],
            }
        ),
        expected_revision=state.revision,
    )
    response = harness.client.get(f"/api/projects/{harness.project_id}/graph-refs")
    assert response.status_code == 200, response.text
    assert response.json()[1]["needs_you"] is True


def test_human_fork_exact_parent_identity_retry_and_archive(manifest, tmp_path) -> None:
    harness = _create_branch_harness(manifest, tmp_path, change="evidence")
    path = f"/api/projects/{harness.project_id}/graph-refs"
    parent_history = cast(BranchHistoryManager, harness.branch)
    service = cast(ProjectService, harness.service)
    parent = parent_history.head_ref()
    request = {
        "request_id": str(uuid.uuid4()),
        "at": f"{parent.target.key}@{parent.revision}",
        "name": "human-fork",
    }
    response = harness.client.post(path, json=request)
    assert response.status_code == 201, response.text
    fork = response.json()
    assert fork["episode_id"] is None and fork["current_episode_id"] is None
    assert fork["parent_head"] == parent.model_dump(mode="json")
    assert fork["branch_id"] == request["request_id"]
    assert harness.client.post(path, json=request).json() == fork
    history = service.history.branch(fork["branch_id"])
    assert history.materialize().state.nodes == parent_history.materialize().state.nodes
    record = harness.store.graph_branch(harness.project_id, fork["branch_id"])
    assert record is not None and record.owner_episode_id is None
    assert record.created_by == f"human:{request['request_id']}"
    assert len(harness.store.episodes(harness.project_id, limit=None)) == 1
    assert (
        harness.client.put(
            f"{path}/{fork['branch_id']}/name", json={"name": "renamed-fork"}
        ).status_code
        == 200
    )
    retried = harness.client.post(path, json=request)
    assert retried.status_code == 201, retried.text
    assert retried.json()["branch_id"] == fork["branch_id"]
    assert retried.json()["name"] == "renamed-fork"
    for archived in (True, False):
        response = harness.client.put(
            f"{path}/{fork['branch_id']}/archived", json={"archived": archived}
        )
        assert response.status_code == 200, response.text
        assert (
            harness.store.branch_admits_new_work(harness.project_id, fork["branch_id"])
            is not archived
        )
    listed = {ref["branch_id"]: ref for ref in harness.client.get(path).json()}
    assert listed[fork["branch_id"]]["name"] == "renamed-fork"


def test_human_fork_past_head_has_no_later_nodes_and_changes_start_at_parent(
    manifest, tmp_path
) -> None:
    from rcp.api.graph_changes import branch_changes

    harness = _create_branch_harness(manifest, tmp_path, change="evidence")
    parent_history = cast(BranchHistoryManager, harness.branch)
    service = cast(ProjectService, harness.service)
    parent = parent_history.branch_metadata().base_head
    path = f"/api/projects/{harness.project_id}/graph-refs"
    response = harness.client.post(
        path,
        json={
            "request_id": str(uuid.uuid4()),
            "at": f"{parent_history.graph_target.key}@{parent.revision}",
        },
    )
    assert response.status_code == 201, response.text
    fork = response.json()
    history = service.history.branch(fork["branch_id"])
    assert "ev/branch-result" not in history.materialize().state.nodes
    service = service.for_graph_target(history.graph_target)
    _, changes = branch_changes(service)
    assert changes.base_head.target == parent_history.graph_target
    assert changes.base_head.revision == parent.revision
    assert changes.nodes == [] and changes.edges == []


@pytest.mark.parametrize("at", ["main@head", "@0", "main@999999", "branch:missing@0"])
def test_human_fork_refuses_unbound_or_missing_head(manifest, tmp_path, at) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = signed_in_client(app)
    response = client.post(
        f"/api/projects/{app.state.default_project_id}/graph-refs",
        json={"request_id": str(uuid.uuid4()), "at": at},
    )
    assert response.status_code in {404, 422}, response.text
    assert app.state.background_tasks.store.graph_branches(app.state.default_project_id) == []


@pytest.mark.parametrize("phase", ["resolve", "create_graph_fork", "_branch_ref_for_http"])
def test_human_fork_unavailable_state_is_retryable(manifest, tmp_path, monkeypatch, phase):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    client = signed_in_client(app)
    history = app.state.catalog.open(project_id).history
    request = {"request_id": str(uuid.uuid4()), "at": f"main@{history.head_ref().revision}"}
    path = f"/api/projects/{project_id}/graph-refs"

    def unavailable(*_args, **_kwargs):
        raise StateUnavailable("Canonical state is temporarily unavailable.")

    with monkeypatch.context() as failure:
        failure.setattr(
            type(history.history_engine) if phase == "resolve" else episode_routes,
            phase,
            unavailable,
        )
        response = client.post(path, json=request)
    assert response.status_code == 503, response.text
    response = client.post(path, json=request)
    assert response.status_code == 201, response.text
    assert response.json()["branch_id"] == request["request_id"]
    assert len(app.state.background_tasks.store.graph_branches(project_id)) == 1


def test_human_fork_experiments_record_and_share_branch_isolation(manifest, tmp_path) -> None:
    from .test_episode_lifecycle_acceptance import _add_worker_seat

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    store = app.state.background_tasks.store
    for node_id in ("exp/fork", "exp/fork-2", "exp/fork-3"):
        _add_worker_seat(app, node_id=node_id, title=node_id, objective="Run on a human fork.")
    head = app.state.catalog.open(project_id).history.head_ref()
    with signed_in_client(app) as client:
        fork = client.post(
            f"/api/projects/{project_id}/graph-refs",
            json={"request_id": str(uuid.uuid4()), "at": f"main@{head.revision}"},
        )
        assert fork.status_code == 201, fork.text
    target = GraphTargetRef(kind="branch", branch_id=fork.json()["branch_id"])

    def admit(node_id: str):
        return _admit_fork_experiment(app, store, project_id, target, node_id)

    first = admit("exp/fork")
    episode = store.episode(first.episode_id)
    assert episode is not None and episode.graph_isolation is True
    assert first.request["graph_isolation"] is episode.graph_isolation
    assert first.request["isolation_owner_episode_id"] == episode.isolation_owner_episode_id
    # A Run admitted before the first launch binds the fork already shares its owner.
    second = admit("exp/fork-2")
    assert second.request["isolation_owner_episode_id"] == first.episode_id
    store.create_episode_isolation(
        project_id,
        EpisodeIsolation(owner_episode_id=first.episode_id, graph_branch_id=target.branch_id),
    )
    third = admit("exp/fork-3")
    assert third.request["isolation_owner_episode_id"] == first.episode_id


def _admit_fork_experiment(app, store, project_id, target, node_id):
    from rcp.service import RunRequest
    from rcp.storage import AgentTaskRecord

    episode_id = str(uuid.uuid4())
    head = app.state.catalog.open(project_id).history.head_ref()

    request = RunRequest(
        provider="codex",
        run_on="laptop",
        run_truth_scope=["repo-a"],
        chat_scope="node",
        chat_id=episode_id,
        node_id=node_id,
        message="Run it.",
        mode="work",
        trigger="experiment_run",
        patch_kind="experiment_loop",
        control_node_id=node_id,
        control_revision=head.revision,
        control_episode_id=episode_id,
        control_invocation=1,
        control_invocation_ceiling=3,
        control_completion_criteria=["The run finishes."],
    )
    assert request.graph_isolation is False
    now = store.now()
    return store.create_experiment_episode_with_invocation(
        AgentTaskRecord(
            operation_id=str(uuid.uuid4()),
            project_id=project_id,
            episode_id=episode_id,
            graph_target=target,
            kind="node_chat",
            status="queued",
            request=request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="Queued.",
            authorized_by=authorized_human(app),
        )
    )


@pytest.mark.parametrize("destination_kind", ["main", "branch"])
@pytest.mark.parametrize("conflict", [False, True])
def test_human_fork_merge_uses_parent_without_episode_or_budget(
    manifest, tmp_path, monkeypatch, destination_kind, conflict
):
    from rcp.agents.hidden_read import host_facts
    from rcp.core.models import Patch
    from rcp.history import HistoryManager
    from rcp.runs.branch_merge_admission import recover_branch_merge_task

    from .helpers import unchecked_write_authority, wait_for_task
    from .test_branch_merge_api import _AnswersWritingLauncher

    harness = _create_branch_harness(manifest, tmp_path, change="none")
    monkeypatch.setattr(
        "rcp.agents.hidden_read.host_facts",
        lambda paths: {**host_facts(paths), "home": str(tmp_path / "execution-home")},
    )
    history = HistoryManager(
        harness.service.manifest,
        harness.service.history.workspace,
        write_authority=unchecked_write_authority(),
    )
    destination = (
        history if destination_kind == "main" else history.branch(harness.branch.branch_id)
    )
    parent = destination.head_ref()
    refs = f"/api/projects/{harness.project_id}/graph-refs"
    if destination_kind == "main":
        # An orchestrator's fork of main has no owned destination; a member merges it.
        from rcp.runs.graph_forks import create_graph_fork

        fork_id = str(uuid.uuid4())
        create_graph_fork(
            harness.service.history,
            harness.store,
            branch_id=fork_id,
            owner_episode_id=harness.episode.episode_id,
            project_id=harness.project_id,
            parent_head=parent,
            created_by=f"orchestrator:{harness.episode.episode_id}",
            authorized_by=harness.episode.authorized_by,
        )
    else:
        response = harness.client.post(
            refs,
            json={"request_id": str(uuid.uuid4()), "at": f"{parent.target.key}@{parent.revision}"},
        )
        assert response.status_code == 201, response.text
        fork_id = response.json()["branch_id"]
    source = history.branch(fork_id)
    operations = [
        {
            "op": "update_nodes",
            "nodes": [{"id": "blk/merge-ready", "changes": {"title": "Fork choice"}}],
        }
    ]
    payload = dict(
        kind="work",
        author="agent",
        producer="agent",
        summary="Fork change",
        run_truth_scope=["repo-a"],
        ops=operations,
    )
    source.append(Patch.model_validate(payload))
    if conflict:
        destination.append(
            Patch.model_validate(
                {
                    **payload,
                    "ops": [
                        {
                            "op": "update_nodes",
                            "nodes": [
                                {"id": "blk/merge-ready", "changes": {"title": "Parent choice"}}
                            ],
                        }
                    ],
                }
            )
        )

    def fork_row():
        return next(row for row in harness.client.get(refs).json() if row["branch_id"] == fork_id)

    assert fork_row()["merge_eligible"] is True
    launcher = _AnswersWritingLauncher()
    monkeypatch.setattr(harness.app.state.launcher, "stream", launcher.stream)
    before = harness.store.episode_budget_meter(harness.episode.episode_id)
    request = {"request_id": str(uuid.uuid4())}
    route = f"{refs}/{fork_id}/merge"
    before_preview_tasks = harness.store.agent_tasks(harness.project_id)
    before_preview_head = destination.head_ref()
    preview_response = harness.client.get(route + "-preview")
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["needs_agent"] is conflict
    assert set(preview["graph"]) == {"direct", "conflicts", "reviews", "paths"}
    assert preview["graph"]["conflicts"] == int(conflict)
    [title_path] = [path for path in preview["graph"]["paths"] if path["field_path"] == "title"]
    assert set(title_path) == {
        "entity",
        "id",
        "field_path",
        "base",
        "branch",
        "main",
        "delivered",
        "conflict",
        "needs_agent",
        "needs_proposal",
    }
    assert title_path["branch"] == "Fork choice"
    assert title_path["main"] == ("Parent choice" if conflict else "Merge gate")
    assert title_path["conflict"] is conflict
    assert harness.store.agent_tasks(harness.project_id) == before_preview_tasks
    assert destination.head_ref() == before_preview_head
    assert not source.merge_receipts()
    if destination_kind == "branch" and not conflict:

        def crash_before_receipt(*_args, **_kwargs):
            raise OSError("Injected crash after destination commit")

        with monkeypatch.context() as crash:
            crash.setattr(BranchHistoryManager, "write_merge_receipt", crash_before_receipt)
            with pytest.raises(OSError):
                harness.client.post(route, json=request)
        [pending] = harness.store.recoverable_bound_merge_tasks()
        harness.store.fail_agent_task(pending.operation_id, "Crash after commit")
        [pending] = harness.store.recoverable_bound_merge_tasks()
        recovered = recover_branch_merge_task(
            harness.app.state.background_tasks, harness.service, pending
        )
        assert recovered["status"] == "succeeded"
    response = harness.client.post(route, json=request)
    assert response.status_code == 200, response.text
    task = wait_for_task(harness.store, response.json()["operation_id"], expect="succeeded")
    assert task.episode_id is None
    assert task.graph_target == source.graph_target
    binding = task.request["merge_binding"]
    assert isinstance(binding, dict)
    assert binding["destination"] == parent.target.model_dump(mode="json")
    assert launcher.calls == int(conflict)
    assert harness.store.episode_budget_meter(harness.episode.episode_id) == before
    assert destination.state().nodes["blk/merge-ready"].title == "Fork choice"
    head = destination.head_ref()
    retry = harness.client.post(route, json=request)
    assert retry.status_code == 200, retry.text
    assert retry.json()["operation_id"] == task.operation_id
    assert retry.json()["status"] == "succeeded"
    assert destination.head_ref() == head
    assert source.merge_receipts()[0].result_main_head.target == parent.target
    assert (fork_row()["merge_state"], fork_row()["merge_eligible"]) == ("merged", False)


@pytest.mark.parametrize("refusal", ["episode_owned", "archived_destination"])
def test_human_fork_merge_refuses_episode_branch_or_archived_parent(manifest, tmp_path, refusal):
    harness = _create_branch_harness(manifest, tmp_path, change="evidence")
    refs = f"/api/projects/{harness.project_id}/graph-refs"
    if refusal == "episode_owned":
        branch_id = harness.branch.branch_id
    else:
        parent = harness.branch.head_ref()
        response = harness.client.post(
            refs,
            json={"request_id": str(uuid.uuid4()), "at": f"{parent.target.key}@{parent.revision}"},
        )
        assert response.status_code == 201, response.text
        branch_id = response.json()["branch_id"]
        harness.store.set_branch_archived(
            harness.project_id, harness.branch.branch_id, archived=True
        )
    before = len(harness.store.episode_tasks(harness.episode.episode_id))
    response = harness.client.post(
        f"{refs}/{branch_id}/merge", json={"request_id": str(uuid.uuid4())}
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] in {
        "merge_source_is_episode_branch",
        "merge_destination_archived",
    }
    assert len(harness.store.episode_tasks(harness.episode.episode_id)) == before
