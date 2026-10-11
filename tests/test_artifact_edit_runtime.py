from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from rcp.agents import AgentEvent, AgentProcessControl
from rcp.background import AgentTaskExecution
from rcp.runs.session_master import record_session_master
from rcp.runs.tasks.artifact_edit import stream_artifact_edit_run
from rcp.service import RunRequest
from rcp.storage import AgentTaskRecord
from rcp.transport import StateUnavailable
from tests.helpers import signed_in_client

from .helpers import create_named_app, fabricated_authorizer
from .test_unified_artifacts import _publish_artifact, _stored_artifact


def _setup(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    source = _stored_artifact(app, "origin", "chart.html", b"<p>original</p>")
    base = _publish_artifact(store, source.artifact_id, b"<p>base</p>")
    stage = tmp_path / "data" / "run-stage" / "stage"
    workspace = stage / "workspace"
    workspace.mkdir(parents=True)
    origin_request = RunRequest(
        provider="codex",
        reasoning="medium",
        model="",
        run_on="laptop",
        chat_scope="project",
        chat_id="chat-edit",
        message="Create chart",
        mode="discuss",
        session_id="native-session",
    )
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="origin",
            project_id=app.state.default_project_id,
            kind="project_chat",
            status="succeeded",
            request=origin_request.model_dump(mode="json"),
            native_session_id="native-session",
            stage_root=str(stage),
            created_at=now,
            updated_at=now,
            status_message="Done",
        )
    )
    store.record_chat_stage_layout("origin", stage_root=str(stage), workspace_root=str(workspace))
    request = origin_request.model_copy(update={"message": "Update the chart"})
    request = RunRequest.model_validate(
        {
            **request.model_dump(mode="json"),
            "artifact_context": {"operation_id": "origin", "artifact_id": source.artifact_id},
            "artifact_edit": {
                "artifact_id": source.artifact_id,
                "base_version": base.version_id,
                "source_name": source.name,
                "media_type": source.media_type,
                "operation_id": "edit",
                "staged_scope_id": "edit",
                "origin_operation_id": "origin",
                "launch_kind": "revoking",
                "stage_root": str(stage),
            },
        }
    )
    tasks = app.state.background_tasks
    tasks.admit_provider_task = lambda *_args, **_kwargs: None
    tasks._spawn_record = lambda record, *_args, **_kwargs: record
    record = tasks.start(
        app.state.default_project_id,
        "artifact_edit",
        request,
        authorized_by=fabricated_authorizer("Editor"),
    )
    assert record.kind == "artifact_edit" and record.native_session_id == request.session_id
    assert record.stage_root == str(stage)
    assert record.request["artifact_edit"] == request.artifact_edit.model_dump(mode="json")
    store.mark_agent_task_running(record.operation_id)
    execution = AgentTaskExecution(
        operation_id="edit",
        store=store,
        control=AgentProcessControl(),
        stage_root=str(stage),
    )
    return app, request, execution, source, workspace


def _edit_request(request, **changes):
    return request.model_copy(
        update={
            "artifact_edit": request.artifact_edit.model_copy(update=changes),
        }
    )


def _edit_execution(execution, request, operation_id, **changes):
    record = execution.store.agent_task(execution.operation_id).model_copy(
        update={
            "operation_id": operation_id,
            "request": request.model_dump(mode="json"),
            **changes,
        }
    )
    execution.store.create_artifact_edit_task(record)
    return AgentTaskExecution(
        operation_id=operation_id,
        store=execution.store,
        control=AgentProcessControl(),
        stage_root=record.stage_root,
    )


def _stage(app, request, execution, workspace):
    from rcp.runs.chat import _prepare_local_artifact_directory, stage_artifact_context
    from rcp.runs.session_artifacts import sync_session_artifacts

    directory = _prepare_local_artifact_directory(workspace, execution.operation_id, reuse=False)
    staged = stage_artifact_context(
        app.state.service,
        request,
        execution,
        local_stage=workspace.parent,
        remote_stage=None,
        edit_path=sync_session_artifacts(execution, request, directory, None),
    )
    return directory, staged


def _finalize(app, request, execution, directory):
    from rcp.runs.session_artifacts import publish_session_artifacts

    return publish_session_artifacts(
        execution, request, directory, None, artifacts=[], service=app.state.service
    )


class _EditLauncher:
    def __init__(
        self,
        *,
        before_edit=None,
        fail=False,
        session="native-session",
        late_event=None,
        scope="edit",
    ):
        self.before_edit = before_edit
        self.fail = fail
        self.session = session
        self.late_event = late_event
        self.scope = scope
        self.calls = []
        self.before = []

    async def stream(self, provider, prompt, **kwargs):
        self.calls.append((provider, kwargs))
        turns = Path(kwargs["cwd"]) / "turns"
        # The commented artifact's tracked file, in its origin turn's folder.
        target = turns / "origin" / "artifacts" / "chart.html"
        self.before.append(target.read_bytes())
        if self.before_edit:
            self.before_edit()
        target.write_bytes(b"<p>edited</p>")
        (turns / self.scope / "artifacts" / "extra.txt").write_text("additional artifact")
        yield AgentEvent(event="session", session_id=self.session)
        if self.fail == "no_result":
            return
        if self.fail:
            yield AgentEvent(event="error", text="Provider failed")
            return
        yield AgentEvent(event="answer", text="Updated the chart.")
        if self.late_event:
            yield AgentEvent(event=self.late_event, text="Interrupted after answer")
        yield AgentEvent(event="done")


async def _run(app, request, execution, launcher, tmp_path):
    return [
        AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
        async for frame in stream_artifact_edit_run(
            app.state.service,
            launcher,
            request,
            tmp_path / "data",
            execution,
        )
    ]


@pytest.mark.parametrize("undo", [False, True])
@pytest.mark.asyncio
async def test_revoking_edit_uses_recorded_workspace_and_publishes_cas(
    manifest,
    tmp_path,
    undo,
):
    app, request, execution, source, workspace = _setup(manifest, tmp_path)
    store = execution.store
    (workspace / "patch.json").write_text("stale patch")
    launcher = _EditLauncher(
        before_edit=(lambda: store.undo_artifact(source.artifact_id)) if undo else None
    )
    events = await _run(app, request, execution, launcher, tmp_path)
    assert not any(event.event == "error" for event in events)
    assert events[-1].event == "done"
    assert launcher.before == [b"<p>base</p>"]
    kwargs = launcher.calls[0][1]
    assert kwargs["cwd"] == workspace
    assert kwargs["capability"] == "discuss"
    assert kwargs["write_dirs"] == []
    assert kwargs["write_scope"] is None
    assert not (workspace / "patch.json").exists()
    assert store.agent_task_contract("edit", "session_master") is None
    artifacts = [event.artifact for event in events if event.event == "artifact"]
    assert {artifact.name for artifact in artifacts} == {"extra.txt", "chart.html"}
    edited = next(artifact for artifact in artifacts if artifact.name == "chart.html")
    assert (edited.artifact_id == source.artifact_id) is not undo
    assert store.read_artifact_bytes(source.artifact_id) == (
        b"<p>original</p>" if undo else b"<p>edited</p>"
    )
    assert store.read_artifact_bytes(edited.artifact_id) == b"<p>edited</p>"
    from rcp.api.tasks import _read_agent_artifact_bytes

    store.complete_agent_task(
        "edit",
        applied_revision=None,
        result={"artifacts": [artifact.model_dump(mode="json") for artifact in artifacts]},
    )
    for artifact in artifacts:
        _, data = _read_agent_artifact_bytes(
            store, app.state.default_project_id, "edit", artifact.artifact_id, "download"
        )
        assert data == store.read_artifact_bytes(artifact.artifact_id)


@pytest.mark.parametrize("failure", ["error", "session", "no_result"])
@pytest.mark.asyncio
async def test_failed_edit_keeps_the_tracked_file_and_retry_continues_it(
    manifest, tmp_path, failure
):
    app, request, execution, source, workspace = _setup(manifest, tmp_path)
    failed = _EditLauncher(
        fail=failure if failure != "session" else False,
        session="wrong-session" if failure == "session" else "native-session",
    )
    events = await _run(app, request, execution, failed, tmp_path)
    assert any(event.event == "error" for event in events)
    assert not any(event.event == "done" for event in events)
    assert execution.store.read_artifact_bytes(source.artifact_id) == b"<p>base</p>"
    assert (
        workspace / "turns" / "origin" / "artifacts" / source.name
    ).read_bytes() == b"<p>edited</p>"
    store = execution.store
    store.fail_agent_task(execution.operation_id, "Provider failed")
    record = app.state.background_tasks.retry(execution.operation_id)
    assert record.operation_id != execution.operation_id
    assert record.parent_operation_id == execution.operation_id
    assert record.stage_root == execution.stage_root
    assert record.request["artifact_edit"] == request.artifact_edit.model_dump(mode="json")
    execution = AgentTaskExecution(
        operation_id=record.operation_id,
        store=store,
        control=AgentProcessControl(),
        stage_root=record.stage_root,
        continuation="retry",
    )
    retry = _EditLauncher(scope=record.operation_id)
    events = await _run(app, request, execution, retry, tmp_path)
    assert retry.before == [b"<p>edited</p>"]
    assert events[-1].event == "done"
    assert execution.store.read_artifact_bytes(source.artifact_id) == b"<p>edited</p>"


def test_comment_route_refuses_busy_session_but_undo_remains_available(manifest, tmp_path):

    app, _request, execution, source, _workspace = _setup(manifest, tmp_path)
    record_session_master(
        execution.store,
        "origin",
        "Operational master",
    )
    client = signed_in_client(app)
    path = f"/api/projects/{app.state.default_project_id}/artifacts/{source.artifact_id}"
    refused = client.post(path + "/comments", json={"comments": [{"text": "Change this chart"}]})
    assert refused.status_code == 409
    undone = client.post(path + "/undo", json={})
    assert undone.status_code == 200
    assert execution.store.read_artifact_bytes(source.artifact_id) == b"<p>original</p>"
    assert execution.store.agent_task(execution.operation_id).status == "running"
    original = client.post(path + "/undo", json={})
    assert original.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("late_event", [None, "error", "paused"])
async def test_ordinary_discuss_edit_preserves_frozen_master(
    manifest, tmp_path, monkeypatch, late_event
):

    import rcp.runs.tasks.discuss as discuss
    from rcp.runs.session_master import session_master_label

    from .helpers import append_fixture_patch, seed_patch

    app, request, execution, source, workspace = _setup(manifest, tmp_path)
    append_fixture_patch(app.state.service, seed_patch())
    store = execution.store
    store.fail_agent_task("edit", "Fixture replaced with Discuss admission")
    frozen = "Frozen child boundary and original chat context."
    digest = record_session_master(store, "origin", frozen)
    request = _edit_request(
        request,
        operation_id="discuss-edit",
        launch_kind="discuss",
        master_operation_id="origin",
        master_sha256=digest,
        master_path=session_master_label("chat-master", frozen),
    )
    execution = _edit_execution(
        execution, request, "discuss-edit", kind="project_chat", status="running"
    )
    masters = []
    render = discuss.PromptFactory.discuss_turn_prompt

    def capture(**kwargs):
        masters.append(kwargs["master"])
        return render(**kwargs)

    monkeypatch.setattr(discuss.PromptFactory, "discuss_turn_prompt", capture)
    launcher = _EditLauncher(late_event=late_event, scope="discuss-edit")
    events = [
        AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
        async for frame in discuss.stream_discuss_run(
            app.state.service,
            launcher,
            request,
            tmp_path / "data",
            execution,
        )
    ]
    if late_event:
        assert not any(event.event == "done" for event in events)
    else:
        assert not [event.text for event in events if event.event == "error"]
        assert events[-1].event == "done"
    assert launcher.calls[0][1]["capability"] == "discuss"
    assert launcher.calls[0][1]["cwd"] == workspace
    assert masters and not masters[0].bootstrap
    assert hashlib.sha256(Path(masters[0].path).read_bytes()).hexdigest() == digest
    assert (
        hashlib.sha256(store.agent_task_contract("origin", "session_master").encode()).hexdigest()
        == digest
    )
    assert store.agent_task_contract("discuss-edit", "session_master") is None
    assert store.read_artifact_bytes(source.artifact_id) == (
        b"<p>base</p>" if late_event else b"<p>edited</p>"
    )


@pytest.mark.parametrize("fresh", [False, True])
def test_edit_origin_chain_opens_its_owning_workspace(manifest, tmp_path, fresh):
    from rcp.runs.tasks.artifact_edit import _open_stage

    app, request, execution, _source, original_workspace = _setup(manifest, tmp_path)
    store = execution.store
    store.fail_agent_task("edit", "Fixture replaced with origin")
    fresh_root = tmp_path / "fresh-edit" if fresh else original_workspace.parent
    fresh_root.mkdir(exist_ok=True)
    fresh_request = _edit_request(
        request,
        operation_id="fresh-origin",
        fresh_session=fresh,
        stage_root=None if fresh else str(fresh_root),
    )
    _edit_execution(
        execution, fresh_request, "fresh-origin", status="succeeded", stage_root=str(fresh_root)
    )
    current = _edit_request(
        request,
        operation_id="after-fresh",
        origin_operation_id="fresh-origin",
        stage_root=str(fresh_root),
    )
    execution = _edit_execution(
        execution, current, "after-fresh", status="running", stage_root=str(fresh_root)
    )
    _local, _remote, workspace, _machine = _open_stage(
        app.state.service,
        current,
        tmp_path / "data",
        execution,
    )
    assert workspace == (fresh_root if fresh else original_workspace)


@pytest.mark.parametrize("undo", [False, True])
def test_unchanged_edit_does_not_publish_or_fork(manifest, tmp_path, undo):
    app, request, execution, source, _, directory, _ = _staged_edit(manifest, tmp_path)
    store = execution.store
    if undo:
        store.undo_artifact(source.artifact_id)
    before = store.artifact(source.artifact_id)
    versions = store.artifact_versions(source.artifact_id)
    assert _finalize(app, request, execution, directory) == []
    assert store.artifact(source.artifact_id) == before
    assert store.artifact_versions(source.artifact_id) == versions


@pytest.mark.parametrize(
    "damage", ["deleted", "type_changed", "byte_cap", "read_cap", "unverified"]
)
@pytest.mark.asyncio
async def test_finalize_failure_preserves_answer(manifest, tmp_path, monkeypatch, damage):
    app, request, execution, source, workspace = _setup(manifest, tmp_path)

    class DamagedEdit(_EditLauncher):
        async def stream(self, *args, **kwargs):
            async for event in super().stream(*args, **kwargs):
                if event.event == "done":
                    target = workspace / "turns" / "origin" / "artifacts" / "chart.html"
                    if damage == "deleted":
                        target.unlink()
                    elif damage == "read_cap":
                        monkeypatch.setattr(
                            "rcp.runs.session_artifacts.CHAT_ARTIFACT_MAX_FILE_BYTES", 1
                        )
                    elif damage == "unverified":
                        target.unlink()
                        target.symlink_to(workspace / "absent")
                    elif damage == "byte_cap":
                        monkeypatch.setattr(
                            "rcp.storage.artifacts.ARTIFACT_MAX_VERSION_BYTES",
                            len(target.read_bytes()),
                        )
                    else:
                        target.write_bytes(b"\x00\xff\x00")
                yield event

    events = await _run(app, request, execution, DamagedEdit(), tmp_path)
    assert any(event.event == "answer" for event in events)
    assert events[-1].event == "done"
    [receipt] = execution.store.agent_task_artifact_edit_outcomes(["edit"])["edit"]
    assert (
        receipt.payload["reason"]
        == {
            "deleted": "missing",
            "type_changed": "type_changed",
            "byte_cap": "too_large",
            "read_cap": "too_large",
            "unverified": "unverified",
        }[damage]
    )
    assert execution.store.read_artifact_bytes(source.artifact_id) == b"<p>base</p>"
    assert not execution.store.agent_task_has_receipt("edit", "artifact_edit_published")
    if damage == "byte_cap":
        assert (workspace / "turns/origin/artifacts/chart.html").read_bytes() == b"<p>edited</p>"


def test_additional_edit_artifact_retains_episode_provenance(manifest, tmp_path):
    import json

    from rcp.runs.chat import _discover_chat_artifacts

    app, request, execution, source, workspace = _setup(manifest, tmp_path)
    record = execution.store.agent_task("edit")
    saved = record.request
    saved["artifact_edit"]["episode_id"] = "origin-episode"
    with execution.store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET request_json = ? WHERE operation_id = ?",
            (json.dumps(saved), "edit"),
        )
    directory = workspace / "turns" / "edit" / "artifacts"
    directory.mkdir(parents=True)
    (directory / "additional.txt").write_text("additional output")
    artifacts = _discover_chat_artifacts(execution, "edit", directory, None)
    assert len(artifacts) == 1
    assert execution.store.artifact(artifacts[0].artifact_id).episode_id == "origin-episode"
    assert execution.store.agent_task("edit").episode_id is None


def _staged_edit(manifest, tmp_path):
    app, request, execution, source, workspace = _setup(manifest, tmp_path)
    directory, staged = _stage(app, request, execution, workspace)
    return app, request, execution, source, workspace, directory, staged


def _tracked_file(staged):
    return Path(staged.pointer["path"])


@pytest.mark.parametrize("failure", [OSError("disk full"), StateUnavailable("offline")])
@pytest.mark.parametrize("phase", ["read", "publish"])
def test_transient_publish_failure_keeps_the_change_for_a_later_turn_end(
    manifest, tmp_path, monkeypatch, failure, phase
):
    import rcp.runs.session_artifacts as module

    app, request, execution, source, _, directory, staged = _staged_edit(manifest, tmp_path)
    _tracked_file(staged).write_bytes(b"<p>edited</p>")
    owner = module if phase == "read" else execution.store
    method = "read_local_regular_file" if phase == "read" else "publish_artifact_version"
    original = getattr(owner, method)

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(owner, method, fail)
    assert _finalize(app, request, execution, directory) == []
    assert not execution.store.agent_task_artifact_edit_outcomes(["edit"])
    monkeypatch.setattr(owner, method, original)
    _finalize(app, request, execution, directory)
    assert execution.store.read_artifact_bytes(source.artifact_id) == b"<p>edited</p>"


@pytest.mark.parametrize("changed", [False, True])
def test_turn_start_resyncs_only_an_untouched_tracked_file(manifest, tmp_path, changed):
    from rcp.runs.session_artifacts import sync_session_artifacts

    app, request, execution, source, workspace = _setup(manifest, tmp_path)
    store = execution.store
    tracked = workspace / "turns" / "origin" / "artifacts" / source.name
    tracked.parent.mkdir(parents=True)
    # The origin turn's file still holds its first version; the human now sees "base".
    tracked.write_bytes(b"<p>agent</p>" if changed else b"<p>original</p>")
    plain = request.model_copy(update={"artifact_context": None, "artifact_edit": None})
    directory = workspace / "turns" / "next" / "artifacts"
    assert sync_session_artifacts(execution, plain, directory, None) is None
    assert tracked.read_bytes() == (b"<p>agent</p>" if changed else b"<p>base</p>")
    store.undo_artifact(source.artifact_id)
    sync_session_artifacts(execution, plain, directory, None)
    assert tracked.read_bytes() == (b"<p>agent</p>" if changed else b"<p>original</p>")


@pytest.mark.parametrize("fork", [False, True])
def test_live_resolution_failure_does_not_record_publish_completion(
    manifest, tmp_path, monkeypatch, fork
):
    import rcp.live_artifact_runtime as live

    app, request, execution, source, _, directory, staged = _staged_edit(manifest, tmp_path)
    _tracked_file(staged).write_bytes(b"<p>edited</p>")
    if fork:
        execution.store.undo_artifact(source.artifact_id)
    calls = []

    def resolve(*args):
        calls.append(args[2:])
        if len(calls) == 1:
            raise OSError("interrupted")

    monkeypatch.setattr(live, "resolve_artifact_live_version", resolve)
    assert _finalize(app, request, execution, directory) == []
    assert not execution.store.agent_task_has_receipt("edit", "artifact_edit_published")
    _finalize(app, request, execution, directory)
    assert calls[0] == calls[1]
    assert execution.store.agent_task_has_receipt("edit", "artifact_edit_published")
    published_id, published_version = calls[0]
    versions = execution.store.artifact_versions(published_id)
    _tracked_file(staged).write_bytes(b"<p>changed retry bytes</p>")
    _finalize(app, request, execution, directory)
    assert execution.store.read_artifact_bytes(published_id) == b"<p>edited</p>"
    assert execution.store.artifact(published_id).current_version == published_version
    assert execution.store.artifact_versions(published_id) == versions


def test_in_place_edit_turn_lists_the_published_artifact_on_replay(manifest, tmp_path):
    # Serving is pinned by test_revoking_edit_uses_recorded_workspace_and_publishes_cas.
    app, request, execution, source, _, directory, staged = _staged_edit(manifest, tmp_path)
    _tracked_file(staged).write_bytes(b"<p>edited</p>")
    artifacts = _finalize(app, request, execution, directory)
    assert [artifact.artifact_id for artifact in artifacts] == [source.artifact_id]
    assert _finalize(app, request, execution, directory) == artifacts


def test_emptying_edit_publishes_and_lists_the_artifact(manifest, tmp_path):
    # Descriptor sizes are positive or unknown; an emptied artifact must not fail the turn.
    app, request, execution, source, _, directory, staged = _staged_edit(manifest, tmp_path)
    _tracked_file(staged).write_bytes(b"")
    artifacts = _finalize(app, request, execution, directory)
    assert [artifact.artifact_id for artifact in artifacts] == [source.artifact_id]
    assert execution.store.read_artifact_bytes(source.artifact_id) == b""


@pytest.mark.asyncio
async def test_mismatched_native_session_never_publishes(manifest, tmp_path):
    app, request, execution, source, _workspace = _setup(manifest, tmp_path)
    events = await _run(app, request, execution, _EditLauncher(session="wrong-session"), tmp_path)
    assert any(event.event == "error" for event in events)
    assert not any(event.event == "done" for event in events)
    assert execution.store.read_artifact_bytes(source.artifact_id) == b"<p>base</p>"


def test_revoking_edit_without_provider_result_emits_error(manifest, tmp_path):
    from rcp.runs.shared import _ProviderOutcome
    from rcp.runs.tasks.artifact_edit import _settle

    app, request, execution, source, workspace = _setup(manifest, tmp_path)
    outcome = _ProviderOutcome()
    frames = list(
        _settle(app.state.service, request, execution, workspace, None, workspace, outcome)
    )
    events = [
        AgentEvent.model_validate_json(frame.removeprefix("data: ").strip()) for frame in frames
    ]
    assert [event.event for event in events] == ["error"]
    assert outcome.failed


@pytest.mark.parametrize(
    "outcome,legacy",
    [
        ("published", False),
        ("unchanged", False),
        ("failed", False),
        ("forked", False),
        ("published", True),
        ("failed", True),
        ("forked", True),
    ],
)
def test_edit_outcome_projects_on_task_detail_and_list(manifest, tmp_path, outcome, legacy):
    app, request, execution, source, _, directory, staged = _staged_edit(manifest, tmp_path)
    store = execution.store
    path = _tracked_file(staged)
    if outcome == "failed":
        path.unlink()
    elif outcome != "unchanged":
        path.write_bytes(b"<p>edited</p>")
    if outcome == "forked":
        store.undo_artifact(source.artifact_id)
    artifacts = _finalize(app, request, execution, directory)
    [receipt] = store.agent_task_artifact_edit_outcomes(["edit"])["edit"]
    assert receipt.payload["status"] == outcome
    expected = {
        "status": outcome,
        "artifact_id": source.artifact_id,
        "version_id": store.artifact(source.artifact_id).current_version
        if outcome == "published"
        else None,
        "separate_artifact_id": artifacts[0].artifact_id if outcome == "forked" else None,
        "reason": "unverified"
        if legacy and outcome == "failed"
        else "missing"
        if outcome == "failed"
        else None,
    }
    path.write_bytes(b"<p>changed after settlement</p>")
    assert _finalize(app, request, execution, directory) == artifacts
    assert store.agent_task_artifact_edit_outcomes(["edit"])["edit"] == [receipt]
    if legacy:
        payload = dict(receipt.payload)
        payload.pop("status")
        payload.pop("reason", None)
        if outcome == "forked":
            payload["artifact_id"] = payload.pop("separate_artifact_id")
        store.record_agent_task_receipt("edit", receipt.category, payload, tier="summary")
    store.complete_agent_task("edit", applied_revision=None, result={"artifacts": []})
    # Retry tasks project the durable outcome of the original admitted edit too.
    _edit_execution(execution, request, "retry", status="succeeded", result={"artifacts": []})
    if legacy and outcome == "failed":
        # Older failed retries wrote the outcome on the attempt, not the admitted edit.
        with store.connection() as connection:
            connection.execute(
                "DELETE FROM graph_run_receipts WHERE operation_id = ? AND category = ?",
                ("edit", receipt.category),
            )
        store.record_agent_task_receipt("retry", receipt.category, payload, tier="summary")
    with signed_in_client(app) as client:
        base = f"/api/projects/{app.state.default_project_id}/tasks"
        records = client.get(base).json()
        for operation_id in ("retry",) if legacy and outcome == "failed" else ("edit", "retry"):
            detail = client.get(f"{base}/{operation_id}")
            assert detail.status_code == 200
            assert detail.json()["result"]["artifact_edit_outcomes"] == [expected]
            listed = next(record for record in records if record["operation_id"] == operation_id)
            assert listed["result"]["artifact_edit_outcomes"] == [expected]
