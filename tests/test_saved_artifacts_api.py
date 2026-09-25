from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from rcp.artifacts import descriptor_for
from rcp.core.transition_models import GraphTargetRef
from rcp.limits import AGENT_TASK_LIST_MAX_LIMIT
from rcp.runs.chat import _append_chat_exchange
from rcp.runs.episodes.wrapup import EpisodeWrapupSpec, begin_episode_report_wrapup
from rcp.service import RunRequest
from rcp.storage import AgentTaskRecord, AutoResearchChildExperimentRecord, EpisodeReportRecord
from rcp.transport import StateUnavailable

from .helpers import authorized_human, create_named_app
from .test_auto_research_commands import _routed_worker
from .test_branch_chats import _app_branch
from .test_episode_api import create_terminal_auto_episode
from .test_project_membership import _create_project, _team_app


def create_saved_artifact(
    app,
    project_id: str,
    *,
    kept: bool = True,
    graph_target: GraphTargetRef | None = None,
    with_chat: bool = True,
):
    """Real persisted output and bytes for inventory and served-viewer checks."""
    store = app.state.background_tasks.store
    operation_id = str(uuid.uuid4())
    data = b"<!doctype html><h1>Saved comparison</h1>"
    now = store.now()
    artifact = descriptor_for(operation_id, "comparison.html", size_bytes=len(data))
    if kept:
        workspace = app.state.catalog.open(project_id).history.workspace
        filename = workspace.keep_artifact(
            source_name=artifact.name,
            project_name="Research",
            data=data,
            today=datetime.now(UTC).date(),
        )
        artifact = artifact.model_copy(update={"kept_filename": filename, "kept_at": now})
    task = store.create_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id=project_id,
            graph_target=graph_target or GraphTargetRef(),
            kind="project_chat",
            status="succeeded",
            request={"chat_id": str(uuid.uuid4()), "chat_scope": "project"},
            result={"artifacts": [artifact.model_dump(mode="json")]},
            created_at=now,
            updated_at=now,
            status_message="Completed",
        )
    )
    store.record_agent_task_receipt(
        operation_id,
        "operation_created",
        {"kind": "project_chat", "attempt": 1, "has_parent": False, "resumed": False},
    )
    if with_chat:
        _save_source_chat(app, task)
    return task, artifact


def _save_source_chat(app, task):
    service = app.state.catalog.open(task.project_id).for_graph_target(
        task.graph_target, initialize=False
    )
    request = RunRequest(
        chat_id=task.request["chat_id"],
        chat_scope=task.request.get("chat_scope", "project"),
        node_id=task.request.get("node_id"),
        message="Explain this saved comparison.",
    )
    _append_chat_exchange(service, request, "The comparison is ready.", None, None)


def _source_chat_url(project_id, href):
    route = urlsplit(href.removeprefix("#"))
    assert route.path == f"/projects/{project_id}"
    query = parse_qs(route.query)
    assert query["view"] == ["chats"]
    url = f"/api/projects/{project_id}/chats/{query['chat'][0]}"
    return url, {"branch_id": query["branch_id"][0]} if "branch_id" in query else {}


def _keep_task_artifact(app, task):
    """Retain one real HTML output on a finished Work turn; returns its task result."""
    store = app.state.background_tasks.store
    store.record_agent_task_receipt(
        task.operation_id,
        "operation_created",
        {"kind": "node_chat", "attempt": 1, "has_parent": False, "resumed": False},
    )
    data = b"<!doctype html><h1>Episode comparison</h1>"
    artifact = descriptor_for(task.operation_id, "comparison.html", size_bytes=len(data))
    workspace = app.state.catalog.open(task.project_id).history.workspace
    filename = workspace.keep_artifact(
        source_name=artifact.name,
        project_name="Research",
        data=data,
        today=datetime.now(UTC).date(),
    )
    artifact = artifact.model_copy(update={"kept_filename": filename, "kept_at": store.now()})
    return {"artifacts": [artifact.model_dump(mode="json")]}


def _create_chat_report(app, tmp_path, *, parent=None, parent_root=None, with_artifact=False):
    store = app.state.background_tasks.store
    project_id = app.state.default_project_id
    now = store.now()
    episode_id = str(uuid.uuid4())
    request = RunRequest(
        chat_id=str(uuid.uuid4()),
        chat_scope="node",
        node_id="exp/bounded-loop",
        message="Measure the bounded comparison.",
        provider="codex",
        model="",
        reasoning="medium",
        run_on="local",
        mode="work",
        patch_kind="experiment_loop",
        trigger="orchestrator" if parent else "experiment_run",
        control_node_id="exp/bounded-loop",
        control_revision=0,
        control_episode_id=episode_id,
        control_invocation=1,
        control_invocation_ceiling=1,
    )
    task = AgentTaskRecord(
        operation_id=str(uuid.uuid4()),
        project_id=project_id,
        episode_id=episode_id,
        graph_target=parent.graph_target if parent else GraphTargetRef(),
        kind="node_chat",
        status="queued",
        request=request.model_dump(mode="json"),
        created_at=now,
        updated_at=now,
        status_message="Queued",
        authorized_by=authorized_human(app),
    )
    route = None
    if parent:
        route = AutoResearchChildExperimentRecord(
            child_episode_id=episode_id,
            auto_research_episode_id=parent.episode_id,
            project_id=project_id,
            control_node_id=request.node_id,
            state="running",
            request={"goal": request.message},
            goal_sha256=hashlib.sha256(request.message.encode()).hexdigest(),
            parent_operation_id=parent_root.operation_id,
            created_at=now,
            updated_at=now,
        )
    store.create_experiment_episode_with_invocation(task, auto_research_route=route)
    store.checkpoint_agent_task(
        task.operation_id,
        native_session_id=str(uuid.uuid4()),
        stage_root=str(tmp_path / f"stage-{episode_id}"),
    )
    result = _keep_task_artifact(app, task) if with_artifact else {}
    store.complete_agent_task(task.operation_id, applied_revision=None, result=result)
    _save_source_chat(app, task)
    admission = begin_episode_report_wrapup(
        store,
        EpisodeWrapupSpec(
            episode_id=episode_id,
            ending="completed",
            partial=False,
            continuation_operation_id=task.operation_id,
            receipt={},
        ),
    )
    assert admission.task is not None
    attempt = store.allocate_episode_report_attempt(episode_id)
    html = (
        "<!doctype html><title>Reset versus stream · Partial episode report</title>"
        "<h1>Saved episode comparison</h1><svg><title>Timeline</title></svg>"
    )
    report = EpisodeReportRecord(
        report_id=str(uuid.uuid4()),
        episode_id=episode_id,
        attempt_id=attempt.attempt_id,
        allocation_operation_id=admission.task.operation_id,
        ending="completed",
        sha256=hashlib.sha256(html.encode()).hexdigest(),
        html=html,
        created_at=store.now(),
    )
    store.finish_episode_report_ready(attempt.attempt_id, report)
    return task, report


def test_inventory_reopens_old_saved_output_and_archived_episode_report(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    store = app.state.background_tasks.store
    task, artifact = create_saved_artifact(app, project_id)
    create_saved_artifact(app, project_id, kept=False)
    episode, _, report = create_terminal_auto_episode(
        store,
        app.state.catalog.open(project_id).history,
        project_id,
        episode_id="saved-report",
        report_html=(
            "<!doctype html><title>Compaction fidelity passes retrieval checks</title>"
            "<h1>Durable episode report</h1>"
        ),
    )
    assert report is not None
    old = (datetime.now(UTC) - timedelta(days=100)).isoformat()
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET created_at = ?, history_only = 1 WHERE operation_id = ?",
            (old, task.operation_id),
        )
    # Enough newer tasks to push the saved output outside the ordinary task list.
    for index in range(AGENT_TASK_LIST_MAX_LIMIT + 1):
        store.create_agent_task(
            AgentTaskRecord(
                operation_id=f"recent-{index}",
                project_id=project_id,
                kind="refresh",
                status="succeeded",
                request={},
                created_at=store.now(),
                updated_at=store.now(),
                status_message="Completed",
            )
        )
    with TestClient(app) as client:
        assert (
            client.post(
                f"/api/projects/{project_id}/episodes/{episode.episode_id}/archive",
                json={"archived": True},
            ).status_code
            == 200
        )
        response = client.get(f"/api/projects/{project_id}/artifacts")
        assert response.status_code == 200, response.text
        entries = response.json()
        assert len(entries) == 2
        saved = next(entry for entry in entries if entry["kind"] == "artifact")
        assert saved["operation_id"] == task.operation_id
        assert saved["artifact_id"] == artifact.artifact_id
        assert saved["path"] == f"artifacts/{artifact.kept_filename}"
        assert saved["can_open"] is True
        assert saved["episode_mode"] is None
        chat_url, chat_params = _source_chat_url(project_id, saved["source_chat_href"])
        assert client.get(chat_url, params=chat_params).status_code == 200
        assert client.get(saved["viewer_url"]).status_code == 200
        content = client.get(saved["viewer_url"].replace("/viewer", "/content"))
        assert content.status_code == 200
        assert "Saved comparison" in content.text
        retained_report = next(entry for entry in entries if entry["kind"] == "report")
        assert retained_report["id"] == f"report:{report.report_id}"
        assert retained_report["episode_id"] == episode.episode_id
        assert retained_report["created_at"] == report.created_at
        assert retained_report["source_chat_href"] is None
        assert retained_report["episode_mode"] == "auto_research"
        assert store.project_episode_report_summaries(str(uuid.uuid4())) == []
        assert retained_report["name"] == "Compaction fidelity passes retrieval checks"
        assert client.get(retained_report["viewer_url"]).status_code == 200
        assert (
            client.post(
                f"/api/projects/{project_id}/episodes/{episode.episode_id}/report/save"
            ).status_code
            == 200
        )
        assert client.get(f"/api/projects/{project_id}/artifacts").json() == entries


def test_inventory_enforces_membership_and_never_lists_other_project_outputs(tmp_path):
    app, client, _, people, acting = _team_app(tmp_path)
    first = _create_project(client, tmp_path / "first", name="First")
    second = _create_project(client, tmp_path / "second", name="Second")
    task, _ = create_saved_artifact(app, first)
    assert client.get(f"/api/projects/{first}/artifacts").json()[0]["operation_id"] == (
        task.operation_id
    )
    assert client.get(f"/api/projects/{second}/artifacts").json() == []
    acting[0] = people[1].user_id
    assert client.get(f"/api/projects/{first}/artifacts").status_code == 404


def test_legacy_project_url_lists_saved_outputs_with_canonical_viewer_urls(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    store = app.state.background_tasks.store
    create_saved_artifact(app, project_id)
    create_terminal_auto_episode(
        store,
        app.state.catalog.open(project_id).history,
        project_id,
        episode_id="aliased-report",
        report_html="<!doctype html><h1>Retained report</h1>",
    )
    alias = "legacy-project-url"
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO project_aliases(alias_id, canonical_project_id) VALUES (?, ?)",
            (alias, project_id),
        )
    # Reopen to load the durable alias into the catalog's request-path snapshot.
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    with TestClient(app) as client:
        canonical = client.get(f"/api/projects/{project_id}/artifacts")
        legacy = client.get(f"/api/projects/{alias}/artifacts")
        assert canonical.status_code == legacy.status_code == 200
        assert legacy.json() == canonical.json()
        assert {entry["kind"] for entry in legacy.json()} == {"artifact", "report"}
        for entry in legacy.json():
            assert entry["viewer_url"].startswith(f"/api/projects/{project_id}/")
            assert client.get(entry["viewer_url"]).status_code == 200


def test_saved_origins_use_exact_main_and_branch_chats_in_one_scan_per_target(
    manifest, tmp_path, monkeypatch
):
    app, main, branch, _ = _app_branch(manifest, tmp_path)
    project_id = app.state.default_project_id
    main_task, _ = create_saved_artifact(app, project_id)
    branch_task, _ = create_saved_artifact(app, project_id, graph_target=branch.graph_target)
    create_saved_artifact(app, project_id, graph_target=branch.graph_target)
    scans = []
    original = type(main)._canonical_chat_files

    def counted(service):
        scans.append(service.history.graph_target.key)
        return original(service)

    monkeypatch.setattr(type(main), "_canonical_chat_files", counted)
    client = TestClient(app)
    response = client.get(f"/api/projects/{project_id}/artifacts")
    assert response.status_code == 200, response.text
    assert sorted(scans) == sorted(["main", branch.graph_target.key])
    entries = {entry["operation_id"]: entry for entry in response.json()}
    for task in [main_task, branch_task]:
        url, params = _source_chat_url(project_id, entries[task.operation_id]["source_chat_href"])
        transcript = client.get(url, params=params)
        assert transcript.status_code == 200, transcript.text
        assert transcript.json()["graph_target"] == task.graph_target.model_dump(mode="json")
    branch_url, _ = _source_chat_url(
        project_id, entries[branch_task.operation_id]["source_chat_href"]
    )
    assert client.get(branch_url).status_code == 404


@pytest.mark.parametrize("branch_owned", [False, True])
def test_report_links_to_its_concluding_chat_without_reopening_branch_episode_composer(
    manifest, tmp_path, branch_owned, monkeypatch
):
    if branch_owned:
        app, _, parent, parent_root = _app_branch(manifest, tmp_path, include_experiment=True)
    else:
        app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
        parent = parent_root = None
    task, report = _create_chat_report(app, tmp_path, parent=parent, parent_root=parent_root)
    project_id = app.state.default_project_id
    store = app.state.background_tasks.store
    client = TestClient(app)
    # Inventory uses summaries and the concluding operation, never report HTML.
    with monkeypatch.context() as patch:
        patch.setattr(store, "episode_report", lambda _: pytest.fail("inventory read report bytes"))
        response = client.get(f"/api/projects/{project_id}/artifacts")
    assert response.status_code == 200, response.text
    entry = next(entry for entry in response.json() if entry["id"] == f"report:{report.report_id}")
    assert entry["episode_mode"] == "experiment_loop"
    assert client.get(entry["viewer_url"]).status_code == 200
    if branch_owned:
        query = parse_qs(urlsplit(entry["source_chat_href"].removeprefix("#")).query)
        assert query == {
            "view": ["runs"],
            "experiment": [task.request["node_id"]],
            "episode": [task.episode_id],
            "target": ["branch"],
            "branch": [parent.episode_id],
            "parent": [parent.episode_id],
        }
        transcript = client.get(
            f"/api/projects/{project_id}/chats/{task.request['chat_id']}",
            params={"branch_id": parent.episode_id},
        )
        assert transcript.status_code == 200, transcript.text
    else:
        url, params = _source_chat_url(project_id, entry["source_chat_href"])
        assert client.get(url, params=params).status_code == 200


@pytest.mark.parametrize("routed", [True, False])
def test_auto_research_worker_artifact_opens_its_parent_episode_in_runs(manifest, tmp_path, routed):
    app, _, parent, parent_root = _app_branch(manifest, tmp_path)
    project_id = app.state.default_project_id
    store = app.state.background_tasks.store
    worker_id = str(uuid.uuid4())
    task = _routed_worker(
        store,
        parent,
        admitted_by=parent_root,
        worker_id=worker_id,
        seat_node_id="rq/learning-after-shift",
        instruction="Measure recall after the shift.",
    )
    store.checkpoint_agent_task(
        task.operation_id,
        native_session_id=str(uuid.uuid4()),
        stage_root=str(tmp_path / "worker-stage"),
    )
    store.complete_agent_task(
        task.operation_id, applied_revision=None, result=_keep_task_artifact(app, task)
    )
    _save_source_chat(app, task)
    if not routed:
        # Damaged provenance: the episode still claims the turn, but its worker
        # route is gone. Fail closed rather than open the branch composer for it.
        with store.connection() as connection:
            connection.execute(
                "DELETE FROM auto_research_child_work_attempts WHERE operation_id = ?",
                (task.operation_id,),
            )
    client = TestClient(app)
    entries = client.get(f"/api/projects/{project_id}/artifacts").json()
    entry = next(item for item in entries if item["operation_id"] == task.operation_id)
    assert entry["episode_mode"] == "auto_research"
    assert client.get(entry["viewer_url"]).status_code == 200
    if not routed:
        assert entry["source_chat_href"] is None
        return
    route = urlsplit(entry["source_chat_href"].removeprefix("#"))
    assert route.path == f"/projects/{project_id}"
    assert parse_qs(route.query) == {
        "view": ["runs"],
        "mode": ["auto_research"],
        "episode": [parent.episode_id],
    }
    transcript = client.get(
        f"/api/projects/{project_id}/chats/{worker_id}",
        params={"branch_id": parent.episode_id},
    )
    assert transcript.status_code == 200, transcript.text
    assert transcript.json()["graph_target"] == parent.graph_target.model_dump(mode="json")


def test_saved_preview_survives_missing_or_unavailable_source_chat(
    manifest, tmp_path, monkeypatch, caplog
):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    task, _ = create_saved_artifact(app, project_id, with_chat=False)
    client = TestClient(app)
    entry = client.get(f"/api/projects/{project_id}/artifacts").json()[0]
    assert entry["source_chat_href"] is None
    assert client.get(entry["viewer_url"]).status_code == 200
    _save_source_chat(app, task)

    def unavailable(_chat_ids):
        raise StateUnavailable("source chat mirror unavailable")

    service = app.state.catalog.open(project_id)
    monkeypatch.setattr(service, "chat_transcripts", unavailable)
    entry = client.get(f"/api/projects/{project_id}/artifacts").json()[0]
    assert entry["source_chat_href"] is None
    assert entry["can_open"] is True
    assert client.get(entry["viewer_url"]).status_code == 200
    assert "source chat mirror unavailable" in caplog.text


def test_report_provenance_cannot_link_to_another_projects_chat(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = TestClient(app)
    first = app.state.default_project_id
    second = _create_project(client, tmp_path / "second", name="Second")
    foreign, _ = create_saved_artifact(app, second)
    store = app.state.background_tasks.store
    episode, _, _ = create_terminal_auto_episode(
        store,
        app.state.catalog.open(first).history,
        first,
        episode_id="foreign-origin",
        report_html="<!doctype html><h1>Retained report</h1>",
    )
    # A damaged legacy wrap-up must never turn provenance into cross-project navigation.
    with store.connection() as connection:
        connection.execute(
            "UPDATE episode_wrapups SET concluding_operation_id = ? WHERE episode_id = ?",
            (foreign.operation_id, episode.episode_id),
        )
    entries = client.get(f"/api/projects/{first}/artifacts").json()
    assert len(entries) == 1
    assert entries[0]["source_chat_href"] is None
    assert entries[0]["can_open"] is True


def test_report_without_a_subject_uses_a_plain_title_instead_of_its_episode_hash(
    manifest, tmp_path
):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    store = app.state.background_tasks.store
    create_terminal_auto_episode(
        store,
        app.state.catalog.open(project_id).history,
        project_id,
        episode_id="untitled-report",
        starting_instruction=None,
        report_html="<!doctype html><h1>Retained report</h1>",
    )
    entry = TestClient(app).get(f"/api/projects/{project_id}/artifacts").json()[0]
    assert entry["name"] == "Report"
    assert entry["episode_mode"] == "auto_research"
    assert entry["source_chat_href"] is None


def test_kept_artifact_retains_episode_type_and_its_artifact_viewer(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    task, _ = _create_chat_report(app, tmp_path, with_artifact=True)
    client = TestClient(app)
    entries = client.get(f"/api/projects/{project_id}/artifacts").json()
    entry = next(item for item in entries if item["operation_id"] == task.operation_id)
    assert entry["episode_mode"] == "experiment_loop"
    assert entry["kind"] == "artifact"
    assert entry["episode_id"] is None
    assert "/tasks/" in entry["viewer_url"]
    assert client.get(entry["viewer_url"]).status_code == 200
