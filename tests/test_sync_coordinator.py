from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from rcp.sync_coordinator import SyncCoordinator
from rcp.transport import LocalStateWorkspace, SSHStateWorkspace
from tests.helpers import async_wait_until


def _catalog(workspaces):
    return SimpleNamespace(
        workspace_for_display=workspaces.get,
        resolve_project_id=lambda project_id: project_id,
        request_sync=None,
    )


def test_client_lease_union_expiry_and_local_bypass(tmp_path, monkeypatch):
    async def drive():
        now = [0.0]
        workspace = SSHStateWorkspace(tmp_path / "remote", "fixture", "/project")
        local = LocalStateWorkspace(tmp_path / "local", str(tmp_path / "local"))
        calls = []
        monkeypatch.setattr(workspace, "capture", lambda: calls.append(now[0]))
        coordinator = SyncCoordinator(
            _catalog({"p": workspace, "local": local}), clock=lambda: now[0]
        )
        coordinator.start()
        try:
            coordinator.note_demand("first", {"p", "local"}, None)
            await async_wait_until(lambda: len(calls) == 1)
            coordinator.note_demand("first", set(), None)
            coordinator.note_demand("second", {"p"}, None)
            now[0] = 10
            coordinator.note_demand("local", {"local"}, "local")
            await async_wait_until(lambda: len(calls) == 2)
            # The second client's lease expires independently of local heartbeats.
            now[0] = 61
            coordinator.note_demand("local", {"local"}, "local")
            await async_wait_until(lambda: not coordinator._leases)
        finally:
            await coordinator.stop()
        assert calls == [0, 10]
        assert coordinator.sync_health("local")["status"] == "local"
        assert not (tmp_path / "local").exists()
        assert not coordinator._leases

    asyncio.run(drive())


def test_activation_during_capture_coalesces_and_deduplicates_roots(tmp_path, monkeypatch):
    async def drive():
        workspace = SSHStateWorkspace(tmp_path / "remote", "fixture", "/project")
        same_root = SSHStateWorkspace(tmp_path / "remote", "fixture", "/project")
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def capture():
            calls.append(1)
            entered.set()
            assert release.wait(5)

        monkeypatch.setattr(SSHStateWorkspace, "capture", lambda _self: capture())
        coordinator = SyncCoordinator(_catalog({"p": workspace, "alias": same_root}))
        coordinator.start()
        try:
            coordinator.note_demand("first", {"p"}, "p")
            await async_wait_until(entered.is_set)
            coordinator.note_demand("second", {"alias"}, "alias")
            coordinator.note_demand("first", {"p"}, "p")
            await async_wait_until(lambda: any(c.follow_up for c in coordinator._captures.values()))
            assert calls == [1]
            release.set()
            await async_wait_until(lambda: len(calls) == 2)
        finally:
            release.set()
            await coordinator.stop()
        assert calls == [1, 1]

    asyncio.run(drive())


def test_shutdown_drains_transfer_worker(tmp_path, monkeypatch):
    async def drive():
        workspace = SSHStateWorkspace(tmp_path / "remote", "fixture", "/project")
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def capture():
            entered.set()
            assert release.wait(5)
            finished.set()

        monkeypatch.setattr(workspace, "capture", capture)
        coordinator = SyncCoordinator(_catalog({"p": workspace}))
        coordinator.start()
        coordinator.request("p")
        await async_wait_until(entered.is_set)
        stopping = asyncio.create_task(coordinator.stop())
        try:
            await asyncio.sleep(0)
            assert not stopping.done()
            release.set()
            await stopping
            assert finished.is_set()
        finally:
            release.set()
            await stopping

    asyncio.run(drive())


def test_local_display_journey_keeps_existing_service_without_sync(manifest, tmp_path, monkeypatch):
    from tests.helpers import create_named_app, signed_in_client

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "disposable-data")
    project_id = app.state.default_project_id
    catalog = app.state.catalog
    service = catalog.open(project_id)

    def forbid_capture(_workspace):
        raise AssertionError("Local display must never start a remote transfer")

    monkeypatch.setattr(SSHStateWorkspace, "capture", forbid_capture)
    with signed_in_client(app) as client:
        coordinator = app.state.sync_coordinator
        coordinator.note_demand("local-tab", {project_id}, project_id)
        before = catalog.content_generations(
            project_id, ["graph:main", "chats", "paper", "settings"]
        )
        for path in ("", "/chats", "/paper"):
            response = client.get(f"/api/projects/{project_id}{path}")
            assert response.status_code == 200, response.text
        assert catalog.open_display(project_id) is service
        assert catalog.content_generations(project_id, before) == before
        assert coordinator.sync_health(project_id)["status"] == "local"
        assert not coordinator._captures
        assert not coordinator._leases
    assert not list(tmp_path.rglob("*-generations"))


def test_remote_display_never_prepares_and_cannot_lag_its_content_token(
    manifest, tmp_path, monkeypatch
):
    import shutil

    from tests.helpers import create_named_app, signed_in_client

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    catalog = app.state.catalog
    project_id = app.state.default_project_id
    service = catalog.open(project_id)
    workspace = SSHStateWorkspace(tmp_path / "mirror" / ".research", "fixture", "/project")
    catalog._display_workspaces[project_id] = workspace
    workspace.set_sync_request(lambda: catalog.request_sync(project_id))

    def forbid_prepare(*_args):
        raise AssertionError("A display read must not prepare canonical state")

    monkeypatch.setattr("rcp.projects.prepare_state_workspace", forbid_prepare)
    client = signed_in_client(app)
    assert client.get(f"/api/projects/{project_id}").status_code == 503
    assert project_id in app.state.sync_coordinator._requested
    stage = workspace._generations.stage()
    shutil.copytree(service.history.root, stage, dirs_exist_ok=True)
    workspace._generations.publish(stage)
    first = client.get(f"/api/projects/{project_id}")
    assert first.status_code == 200, first.text
    before = catalog.content_generations(project_id, ["paper", "graph:main"])
    with workspace.read_snapshot() as root:
        stage = workspace._generations.stage()
        shutil.copytree(root, stage, dirs_exist_ok=True)
    (stage / "paper").mkdir(exist_ok=True)
    (stage / "paper" / "introduction.md").write_text("New canonical introduction")
    workspace._generations.publish(stage)
    after = catalog.content_generations(project_id, before)
    assert after["paper"] != before["paper"]
    assert after["graph:main"] == before["graph:main"]
    second = client.get(f"/api/projects/{project_id}")
    assert second.status_code == 200, second.text
    assert second.json()["paper"]["content"] == "New canonical introduction"


def test_execution_reloads_authoritative_manifest_before_resolving_write_scope(
    manifest, tmp_path, monkeypatch
):
    import shutil
    from contextlib import nullcontext

    from rcp.runs.chat import _project_write_scope
    from rcp.service import RunRequest
    from rcp.transport.state import RunLockLease
    from tests.helpers import create_named_app

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "execution-data")
    catalog = app.state.catalog
    project_id = app.state.default_project_id
    service = catalog.open(project_id)
    workspace = SSHStateWorkspace(service.history.root, "fixture", "/project")
    service.history.workspace = workspace
    catalog._display_workspaces[project_id] = workspace
    stage = workspace._generations.stage()
    shutil.copytree(service.history.root, stage, dirs_exist_ok=True)
    workspace._generations.publish(stage)
    display = catalog.open_display(project_id)
    previous_path = display.manifest.repository_map["repo-b"].path
    current_path = tmp_path / "current-repository"
    current_path.mkdir()
    current_manifest = manifest.path.read_text().replace(previous_path, str(current_path))
    transactions = []

    def sync_authoritative_manifest():
        transactions.append(True)
        manifest.path.write_text(current_manifest)

    monkeypatch.setattr(workspace, "_sync_remote_tree", sync_authoritative_manifest)
    monkeypatch.setattr(workspace, "_remote_manifest_exists", lambda: True)
    monkeypatch.setattr(
        workspace, "_remote_advisory_lock", lambda _path: nullcontext(RunLockLease("fixture"))
    )
    execution = catalog.open_execution(project_id)
    context = execution.assemble_chat(RunRequest(run_truth_scope=["repo-b"]))
    run_stage = tmp_path / "execution-stage"
    (run_stage / "inputs").mkdir(parents=True)
    scope = _project_write_scope(
        context,
        execution,
        "laptop",
        workspace=run_stage,
        local_stage=run_stage,
        remote_stage=None,
        data_dir=tmp_path / "execution-data",
        execution=None,
        capability="work_auto",
    )
    assert transactions == [True]
    assert display.manifest.repository_map["repo-b"].path == previous_path
    assert scope.repository_roots == [str(current_path)]
