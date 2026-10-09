from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from rcp.core.models import EpisodeIsolation, EpisodeMergeAttempt, GraphBranchMetadata
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.storage import EpisodeRecord
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
        history.create_episode_branch(
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
    assert branch["merge_state"] == "running"
    assert branch["active_merge_task_id"] == task.operation_id
    assert branch["merge_eligible"] is False

    receipt = harness.branch.write_merge_receipt(
        _current_receipt(harness, task_id=task.operation_id)
    )
    harness.store.complete_agent_task(task.operation_id, applied_revision=None, result={})
    response = harness.client.get(path)
    assert response.status_code == 200, response.text
    _, branch = response.json()
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
