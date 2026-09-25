from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rcp.agents import AgentEvent
from rcp.api.episode_branches import ensure_auto_research_graph_target
from rcp.runs.auto_research import AutoResearchStartRequest
from rcp.runs.auto_research_admission import reserve_auto_research

from .helpers import (
    agent_patch_json,
    append_fixture_patch,
    authorized_human,
    create_named_app,
    seed_patch,
    shape_invalid_patch,
    wait_for_task,
)
from .test_api import ScriptedLauncher, _experiment_fixture_patch
from .test_branch_history import _branch_patch
from .test_branch_target_storage import _merge_task


def _app_branch(manifest, tmp_path, *, include_experiment=False):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.catalog.open(app.state.default_project_id)
    append_fixture_patch(service, seed_patch())
    if include_experiment:
        append_fixture_patch(service, _experiment_fixture_patch())
    episode, root, _ = reserve_auto_research(
        app.state.background_tasks,
        app.state.default_project_id,
        AutoResearchStartRequest(
            invocation_ceiling=3,
            provider="codex",
            model="",
            reasoning="medium",
            run_on="laptop",
            run_truth_scope=["repo-a"],
        ),
        authorized_by=authorized_human(app),
        graph_base_head=service.history.head_ref(),
    )
    ensure_auto_research_graph_target(episode, catalog=app.state.catalog)
    app.state.catalog.store.activate_auto_research_reservation(
        episode.episode_id, root.operation_id
    )
    return app, service, episode, root


@pytest.mark.parametrize("kind", ["project_chat", "node_chat"])
def test_branch_discuss_and_work_share_normal_session_during_and_after_episode(
    manifest, tmp_path, monkeypatch, kind
):
    app, main, episode, root = _app_branch(manifest, tmp_path)
    client = TestClient(app)
    store = app.state.catalog.store
    project_id = app.state.default_project_id
    branch_id = episode.episode_id
    branch = main.for_graph_target(episode.graph_target)
    launcher = ScriptedLauncher(
        [{}, {"patch.json": agent_patch_json(_branch_patch("ev/human-review"))}, {}],
        message="The evidence answers the selected research question.",
    )
    monkeypatch.setattr(app.state.launcher, "stream", launcher.stream)
    chat_id = str(uuid.uuid4())
    payload = {
        "chat_id": chat_id,
        "node_id": "rq/learning-after-shift" if kind == "node_chat" else None,
        "message": "Explain this research context.",
        "mode": "discuss",
        "run_truth_scope": ["repo-a"],
    }
    url = f"/api/projects/{project_id}/tasks/{kind}?branch_id={branch_id}"
    first = client.post(url, json=payload)
    assert first.status_code == 202, first.json()
    first_task = wait_for_task(store, first.json()["operation_id"])
    assert first_task.status == "succeeded", first_task.error
    assert first_task.episode_id is None
    assert first_task.graph_target == episode.graph_target
    assert first_task.dispatch_authority.scope.episode_id is None
    assert launcher.launch_kwargs[0]["capability"] == "discuss"

    store.complete_agent_task(root.operation_id, applied_revision=None, result={})
    store.mark_episode_stop_skipped(branch_id)
    second = client.post(
        url,
        json={**payload, "mode": "work", "session_id": first_task.native_session_id},
    )
    assert second.status_code == 202, second.json()
    second_task = wait_for_task(store, second.json()["operation_id"])
    assert second_task.status == "succeeded", second_task.error
    assert second_task.result["graph_update"]["status"] == "applied", second_task.result
    assert second_task.episode_id is None
    assert second_task.graph_target == episode.graph_target
    assert "ev/human-review" in branch.history.state().nodes
    assert "ev/human-review" not in main.history.state().nodes
    assert launcher.resumed_sessions == [None, first_task.native_session_id]
    assert launcher.workspaces[0] == launcher.workspaces[1]
    assert launcher.launch_kwargs[1]["capability"] == "work_auto"

    third = client.post(
        url,
        json={**payload, "session_id": second_task.native_session_id},
    )
    assert third.status_code == 202, third.json()
    assert wait_for_task(store, third.json()["operation_id"]).status == "succeeded"
    transcript_url = f"/api/projects/{project_id}/chats/{chat_id}"
    transcript = client.get(transcript_url, params={"branch_id": branch_id})
    assert transcript.status_code == 200, transcript.json()
    assert transcript.json()["graph_target"] == episode.graph_target.model_dump(mode="json")
    assert [message["mode"] for message in transcript.json()["messages"]] == [
        "discuss",
        "discuss",
        "work",
        "work",
        "discuss",
        "discuss",
    ]
    assert client.get(transcript_url).status_code == 404
    assert client.get(f"/api/projects/{project_id}/chats").json()["items"] == []
    assert [
        item["chat_id"]
        for item in client.get(
            f"/api/projects/{project_id}/chats", params={"branch_id": branch_id}
        ).json()["items"]
    ] == [chat_id]
    branch_tasks = client.get(
        f"/api/projects/{project_id}/tasks", params={"branch_id": branch_id}
    ).json()
    assert all(
        item["graph_target"] == episode.graph_target.model_dump(mode="json")
        for item in branch_tasks
    )
    rejected = client.post(f"/api/projects/{project_id}/tasks/{kind}", json=payload)
    assert rejected.status_code == 422
    assert "another graph target" in rejected.json()["detail"]

    # Old canonical records had no graphTarget. The task ledger still identifies
    # this branch chat after restart; no transcript content becomes task input.
    path = branch.chat_path(
        chat_id, chat_scope="node" if kind == "node_chat" else "project", node_id=payload["node_id"]
    )
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert all(
        record.pop("graphTarget") == episode.graph_target.model_dump(mode="json")
        for record in records
    )
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    assert client.get(transcript_url).status_code == 404
    assert client.get(transcript_url, params={"branch_id": branch_id}).json()[
        "graph_target"
    ] == episode.graph_target.model_dump(mode="json")


@pytest.mark.parametrize("kind", ["project_chat", "node_chat"])
def test_branch_merge_admits_fresh_discuss_without_graph_authority(
    manifest, tmp_path, monkeypatch, kind
):
    app, main, episode, root = _app_branch(manifest, tmp_path)
    client = TestClient(app)
    store = app.state.catalog.store
    store.complete_agent_task(root.operation_id, applied_revision=None, result={})
    store.mark_episode_stop_skipped(episode.episode_id)
    merge = store.create_branch_merge_task(_merge_task(store, episode, "held-merge"))
    branch = main.for_graph_target(episode.graph_target)
    before = branch.history.head_ref()
    launcher = ScriptedLauncher(
        [{"patch.json": agent_patch_json(_branch_patch("ev/discuss-cannot-apply"))}],
        message="This branch records the episode's research context.",
    )
    monkeypatch.setattr(app.state.launcher, "stream", launcher.stream)
    payload = {
        "chat_id": str(uuid.uuid4()),
        "node_id": "rq/learning-after-shift" if kind == "node_chat" else None,
        "message": "Explain the episode graph while it merges.",
        "mode": "discuss",
        "run_truth_scope": ["repo-a"],
    }
    url = f"/api/projects/{episode.project_id}/tasks/{kind}?branch_id={episode.episode_id}"

    response = client.post(url, json=payload)
    assert response.status_code == 202, response.json()
    task = wait_for_task(store, response.json()["operation_id"])
    assert task.status == "succeeded", task.error
    assert task.episode_id is None
    assert task.graph_target == episode.graph_target
    assert task.request["mode"] == "discuss"
    assert task.dispatch_authority.task_contract == "discuss"
    assert launcher.launch_kwargs[0]["capability"] == "discuss"
    assert branch.history.head_ref() == before
    assert "ev/discuss-cannot-apply" not in branch.history.state().nodes
    assert "ev/discuss-cannot-apply" not in main.history.state().nodes
    assert store.agent_task(merge.operation_id).status == "queued"

    refused = client.post(
        url,
        json={**payload, "mode": "work", "session_id": task.native_session_id},
    )
    assert refused.status_code == 409, refused.json()
    assert "merge" in refused.json()["detail"]
    assert launcher.calls == 1


def test_branch_chat_api_requires_existing_canonical_branch_and_explicit_route(manifest, tmp_path):
    app, _main, episode, _root = _app_branch(manifest, tmp_path)
    client = TestClient(app)
    base = f"/api/projects/{app.state.default_project_id}"
    payload = {"chat_id": str(uuid.uuid4()), "message": "Explain this graph."}
    assert (
        client.post(f"{base}/tasks/project_chat?branch_id=invalid", json=payload).status_code == 404
    )
    assert (
        client.post(f"{base}/tasks/project_chat?branch_id={uuid.uuid4()}", json=payload).status_code
        == 404
    )
    assert (
        client.post(f"{base}/tasks/refresh?branch_id={episode.episode_id}", json={}).status_code
        == 422
    )
    assert (
        client.post(
            f"{base}/tasks/project_chat",
            json={**payload, "graph_target": episode.graph_target.model_dump(mode="json")},
        ).status_code
        == 422
    )


@pytest.mark.parametrize("action", ["resume", "retry"])
def test_branch_chat_recovery_uses_original_graph_target(manifest, tmp_path, monkeypatch, action):
    app, main, episode, root = _app_branch(manifest, tmp_path)
    store = app.state.catalog.store
    client = TestClient(app)
    base = f"/api/projects/{episode.project_id}"
    launched_targets: list[dict] = []
    native_session = str(uuid.uuid4())

    async def launch(_provider, prompt, **kwargs):
        launched_targets.append(kwargs)
        yield AgentEvent(event="session", session_id=native_session)
        if len(launched_targets) == 1:
            yield AgentEvent(event="paused", text="Paused at native checkpoint.")
            return
        (Path(kwargs["cwd"]) / "patch.json").write_text(
            agent_patch_json(_branch_patch("ev/recovered-branch-work"))
        )
        yield AgentEvent(event="answer", text="The branch work is complete.")
        yield AgentEvent(event="done")

    monkeypatch.setattr(app.state.launcher, "stream", launch)
    started = client.post(
        f"{base}/tasks/project_chat?branch_id={episode.episode_id}",
        json={"chat_id": str(uuid.uuid4()), "message": "Record the result.", "mode": "work"},
    )
    assert started.status_code == 202, started.json()
    previous = wait_for_task(store, started.json()["operation_id"])
    assert previous.status == "paused", previous.error
    store.complete_agent_task(root.operation_id, applied_revision=None, result={})
    store.mark_episode_stop_skipped(episode.episode_id)
    recovered = client.post(f"{base}/tasks/{previous.operation_id}/{action}")
    assert recovered.status_code == 202, recovered.json()
    task = wait_for_task(store, recovered.json()["operation_id"])
    assert task.status == "succeeded", task.error
    assert task.graph_target == previous.graph_target == episode.graph_target
    assert task.episode_id is None
    assert task.request["mode"] == "work"
    assert (
        "ev/recovered-branch-work"
        in main.for_graph_target(episode.graph_target).history.state().nodes
    )
    assert "ev/recovered-branch-work" not in main.history.state().nodes
    assert store.unsettled_graph_target_tasks(episode.project_id, episode.graph_target) == []


def test_branch_chat_patch_repair_does_not_apply_to_main(manifest, tmp_path, monkeypatch):
    app, main, episode, _root = _app_branch(manifest, tmp_path)
    store = app.state.catalog.store
    client = TestClient(app)
    base = f"/api/projects/{episode.project_id}"
    invalid = agent_patch_json(shape_invalid_patch().model_copy(update={"kind": "work"}))
    launcher = ScriptedLauncher(
        [{"patch.json": invalid}] * 3
        + [{"patch.json": agent_patch_json(_branch_patch("ev/repaired-branch-work"))}],
        message="The work completed; the graph needs repair.",
    )
    monkeypatch.setattr(app.state.launcher, "stream", launcher.stream)
    started = client.post(
        f"{base}/tasks/project_chat?branch_id={episode.episode_id}",
        json={"chat_id": str(uuid.uuid4()), "message": "Record the result.", "mode": "work"},
    )
    assert started.status_code == 202, started.json()
    previous = wait_for_task(store, started.json()["operation_id"])
    assert previous.result["graph_update"]["repairable"], previous.result
    repaired = client.post(f"{base}/tasks/{previous.operation_id}/repair-graph-update")
    assert repaired.status_code == 202, repaired.json()
    task = wait_for_task(store, repaired.json()["operation_id"])
    assert task.status == "succeeded", task.error
    assert task.graph_target == previous.graph_target == episode.graph_target
    assert task.episode_id is None
    assert task.result["graph_update"]["status"] == "applied", task.result
    assert (
        "ev/repaired-branch-work"
        in main.for_graph_target(episode.graph_target).history.state().nodes
    )
    assert "ev/repaired-branch-work" not in main.history.state().nodes


def test_human_branch_experiment_has_own_episode_and_target_bound_recovery(
    manifest, tmp_path, monkeypatch
):
    app, main, branch_episode, root = _app_branch(manifest, tmp_path, include_experiment=True)
    store = app.state.catalog.store
    client = TestClient(app)
    base = f"/api/projects/{branch_episode.project_id}"
    sessions: list[str | None] = []
    native_session = str(uuid.uuid4())

    async def launch(_provider, _prompt, **kwargs):
        sessions.append(kwargs.get("session_id"))
        yield AgentEvent(event="session", session_id=native_session)
        if len(sessions) == 1:
            yield AgentEvent(event="paused", text="Paused at native checkpoint.")
            return
        repository = manifest.repository_map["repo-a"].path
        (Path(kwargs["cwd"]) / "watch.json").write_text(
            json.dumps(
                {
                    "external": [
                        {
                            "check_command": "false",
                            "log_path": str(Path(repository) / "check.log"),
                            "cwd": repository,
                        }
                    ],
                    "graph": [],
                }
            )
        )
        (Path(kwargs["cwd"]) / "patch.json").write_text(
            agent_patch_json(_branch_patch("ev/experiment-review"))
        )
        yield AgentEvent(event="answer", text="The bounded check is complete.")
        yield AgentEvent(event="done")

    monkeypatch.setattr(app.state.launcher, "stream", launch)
    started = client.post(
        f"{base}/experiments/exp%2Fbounded-loop/run?branch_id={branch_episode.episode_id}",
        json={"chat_id": str(uuid.uuid4())},
    )
    assert started.status_code == 202, started.json()
    previous = wait_for_task(store, started.json()["operation_id"])
    assert previous.status == "paused", previous.error
    assert previous.graph_target == branch_episode.graph_target
    assert previous.episode_id not in {None, branch_episode.episode_id}
    experiment = store.episode(previous.episode_id)
    assert experiment.graph_base_head == branch_episode.graph_base_head
    assert store.auto_research_child_experiments(branch_episode.episode_id) == []
    store.complete_agent_task(root.operation_id, applied_revision=None, result={})
    store.mark_episode_stop_skipped(branch_episode.episode_id)
    recovered = client.post(f"{base}/tasks/{previous.operation_id}/resume")
    assert recovered.status_code == 202, recovered.json()
    task = wait_for_task(store, recovered.json()["operation_id"])
    assert task.status == "succeeded", task.error
    assert task.graph_target == branch_episode.graph_target
    assert task.episode_id == previous.episode_id
    assert sessions == [None, native_session]
    assert (
        "ev/experiment-review"
        in main.for_graph_target(branch_episode.graph_target).history.state().nodes
    )
    assert "ev/experiment-review" not in main.history.state().nodes
