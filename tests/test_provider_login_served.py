"""Served API journey: device login completes after the client navigates away."""

from __future__ import annotations

import socket
import threading

import httpx
import uvicorn

from rcp.agents import AgentEvent, ProviderReadiness
from rcp.agents import launcher as launcher_module
from rcp.api.app import create_app
from rcp.runs.provider_sign_in import ProviderSignInRunner
from rcp.service import RunRequest
from rcp.storage import ProjectRecord

from .helpers import fabricated_authorizer, wait_for_task, wait_until
from .test_provider_sign_in import _fake_codex


def test_served_device_login_recovers_queued_work_without_polling(manifest, tmp_path, monkeypatch):
    data = tmp_path / "served-data"
    monkeypatch.setenv("RCP_DATA_DIR", str(data))
    binary_dir = tmp_path / "fake-provider"
    binary_dir.mkdir()
    binary = _fake_codex(binary_dir)
    monkeypatch.setattr(launcher_module, "_discover_local_provider", lambda provider: str(binary))
    app = create_app(data_dir=data)
    services = app.state.services
    store = services.store
    launcher = services.launcher
    monkeypatch.setattr(
        launcher,
        "readiness",
        lambda provider, **_: ProviderReadiness(
            provider=provider,
            installed=False,
            authenticated=False,
            reason="Fake provider only serves the login journey.",
        ),
    )
    store.upsert_project(
        ProjectRecord(
            project_id="project",
            locator=str(manifest.path),
            name="Login journey",
            state_location=str(tmp_path / "state"),
            state_remote=False,
            added_at=store.now(),
        )
    )
    background = services.background_tasks
    calls = []

    async def stream(_project, _kind, _request, execution):
        calls.append(execution.operation_id)
        yield f"data: {AgentEvent(event='done').model_dump_json()}\n\n"

    background.stream = stream
    with monkeypatch.context() as park:
        park.setattr(background, "launch_admitted", store.agent_task)
        queued = background.start(
            "project",
            "project_chat",
            RunRequest(
                provider="codex",
                run_on="local",
                chat_scope="project",
                chat_id="00000000-0000-4000-8000-000000000777",
                message="Inspect results",
                mode="discuss",
            ),
            authorized_by=fabricated_authorizer(),
        )
    store.mark_provider_login_failed(
        "codex", "", generation=0, detail="refresh_token_reused", source="turn"
    )
    assert background.launch_admitted(queued.operation_id).status == "queued"
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    assert port != 8421
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        wait_until(lambda: server.started)
        url = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=url) as client:
            response = client.post("/api/providers/codex/logins/sign-in", json={"host": ""})
            assert response.status_code == 200, response.text
            login_id = response.json()["login_id"]
            wait_until(lambda: services.provider_sign_ins.sign_in_status(login_id).user_code)
            # Navigation away and disconnection happen before device completion.
            page = client.get("/")
            assert page.status_code == 200 and '<div id="root"></div>' in page.text
        (binary_dir / "signed-in").touch()
        wait_for_task(store, queued.operation_id, expect="succeeded")
        wait_until(lambda: services.provider_sign_ins.sign_in_status(login_id).state == "succeeded")
        assert calls == [queued.operation_id]
        services.provider_sign_ins.reconcile_recovery()
        restarted = ProviderSignInRunner(store, launcher, launcher.accounts)
        restarted.resume_account = services.provider_sign_ins.resume_account
        restarted.reconcile_recovery()
        with httpx.Client(base_url=url) as client:
            status = client.get(f"/api/providers/codex/logins/sign-in/{login_id}")
            assert status.status_code == 200 and status.json()["state"] == "succeeded"
            assert "checked" in status.json()["resumed"]
        assert calls == [queued.operation_id]
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive()
