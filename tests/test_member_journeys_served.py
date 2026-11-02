"""Drive member journeys over the served API on the acceptance agent."""

from __future__ import annotations

import json
import logging
import socket
import threading

import httpx
import uvicorn

from rcp import background

from .helpers import TASK_SETTLE_TIMEOUT, create_named_app, wait_until


def _thread_row(client: httpx.Client, thread_id: str) -> dict[str, object]:
    response = client.get("/api/agent-threads")
    assert response.status_code == 200, response.text
    return next(row for row in response.json()["threads"] if row["thread_id"] == thread_id)


def _settled_turn(client: httpx.Client, thread_id: str, *, after: str | None) -> dict[str, object]:
    """The thread's newest turn other than `after`, once the member sees it idle."""

    def probe() -> dict[str, object] | None:
        row = _thread_row(client, thread_id)
        operation_id = row["latest_operation_id"]
        if operation_id in {None, after} or row["live_state"] != "idle":
            return None
        owner = (
            f"/api/projects/{row['project_id']}"
            if row["project_id"]
            else f"/api/agent-threads/{thread_id}"
        )
        response = client.get(f"{owner}/tasks/{operation_id}")
        assert response.status_code == 200, response.text
        return response.json()

    return wait_until(
        probe,
        timeout=TASK_SETTLE_TIMEOUT,
        detail=lambda: f"thread {thread_id} never settled: {_thread_row(client, thread_id)}",
    )


def test_space_handoff_delivers_and_wakes_the_space_thread_once(
    manifest, tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(background, "seconds_until_automatic_launch", lambda: 0.0)
    data_dir = tmp_path / "data"
    app = create_named_app(str(manifest.path), data_dir=data_dir, acceptance_agent=True)
    store = app.state.services.store
    project_id = app.state.default_project_id
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        assert port != 8421
        server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=True))
        worker = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        worker.start()
        try:
            wait_until(lambda: server.started)
            wait_until(lambda: app.state.startup_effect_runtime_event.is_set())
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
                assert (
                    client.post(
                        "/api/owner/redeem", json={"code": store.create_owner_sign_in_code()}
                    ).status_code
                    == 200
                )
                threads = client.get("/api/agent-threads").json()["threads"]
                space_id = next(row["thread_id"] for row in threads if row["kind"] == "space")
                project_thread_id = next(
                    row["thread_id"] for row in threads if row["project_id"] == project_id
                )
                owner = store.agent_thread(space_id).user_id
                stage = data_dir / "agent-threads" / owner / "stages" / f"space-{space_id}"
                stage.mkdir(parents=True)
                (stage / ".rcp-acceptance-agent.json").write_text(
                    json.dumps({"scenario": "space_handoff", "project_id": project_id}),
                    encoding="utf-8",
                )

                sent = client.post(
                    f"/api/agent-threads/{space_id}/turns",
                    json={"message": "Ask the project agent to check the latest run."},
                )
                assert sent.status_code == 202, sent.text
                first = _settled_turn(client, space_id, after=None)
                assert first["operation_id"] == sent.json()["operation_id"]
                assert first["status"] == "succeeded", first.get("error")

                delivered = _settled_turn(client, project_thread_id, after=None)
                assert delivered["status"] == "succeeded", delivered.get("error")

                wake = _settled_turn(client, space_id, after=first["operation_id"])
                assert wake["status"] == "succeeded", wake.get("error")

                transcript = client.get(f"/api/agent-threads/{space_id}/transcript").json()
                assert [message["role"] for message in transcript["messages"]].count(
                    "assistant"
                ) == 2
                assert _thread_row(client, space_id)["latest_operation_id"] == wake["operation_id"]
        finally:
            server.should_exit = True
            worker.join(timeout=TASK_SETTLE_TIMEOUT)
        assert not worker.is_alive()
    assert [(record.scenario, record.action) for record in app.state.launcher.launch_records] == [
        ("space_handoff", "initial"),
        ("space_handoff", "turn"),
        ("space_handoff", "wake"),
    ]
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
