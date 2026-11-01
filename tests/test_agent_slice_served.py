"""Exercise member taps and display-only voice writes over the served API."""

from __future__ import annotations

import logging
import socket
import threading
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import uvicorn

from rcp.core.transition_models import GraphTargetRef
from rcp.storage import QuestionOrigin

from .helpers import TASK_SETTLE_TIMEOUT, create_named_app, wait_until


def test_served_cards_schedules_and_voice(manifest, tmp_path, caplog):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.services.store
    project_id = app.state.default_project_id
    thread_id = str(uuid.uuid4())
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO agent_threads VALUES (?,?,'project',?,?,NULL,NULL)",
            (thread_id, store.local_owner.user_id, project_id, store.now()),
        )
    question = store.create_or_get_question(
        origin=QuestionOrigin(
            owner_kind="chat",
            project_id=project_id,
            owner_id=thread_id,
            operation_id="original-turn",
            provider="codex",
            native_session_id="session",
            stage_root="/stage",
            capability="work",
            write_scope_fingerprint="binding",
            graph_target=GraphTargetRef(kind="main"),
        ),
        key="request",
        question="Allow repository writes?",
        choices=["Approve", "Decline"],
        card={
            "action": {
                "kind": "truth_scope",
                "project_id": project_id,
                "alias": "code",
                "writable": True,
            },
            "pinned": {"writable": False},
        },
    )
    future = datetime.now(UTC) + timedelta(days=1)
    schedule = store.add_agent_schedule(
        thread_id=thread_id,
        operation_id="original-turn",
        key="reminder",
        cron=None,
        at=future.strftime("%Y-%m-%dT%H:%M"),
        timezone="UTC",
        note="Check results",
        next_fire_at=future.isoformat(),
    )
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
                base = f"/api/agent-threads/{thread_id}"
                listed = client.get(f"/api/projects/{project_id}/chats/{thread_id}/questions")
                assert listed.status_code == 200
                card = listed.json()[0]["card"]
                url = f"/api/approval-cards/{question.question_id}"
                decision = {"decision": "decline", "pinned_sha256": card["pinned_sha256"]}
                assert client.post(url, json=decision).json()["outcome"] == "declined"
                assert client.post(url, json=decision).status_code == 409
                assert (
                    client.get(base + "/schedules").json()["schedules"][0]["schedule_id"]
                    == schedule.schedule_id
                )
                assert client.delete(base + f"/schedules/{schedule.schedule_id}").status_code == 204
                entries = {
                    "entries": [
                        {
                            "entry_id": "spoken-1",
                            "speaker": "member",
                            "text": "Hello",
                            "at": store.now(),
                        }
                    ]
                }
                first = client.post(base + "/voice-entries", json=entries)
                assert first.status_code == 200, first.text
                assert first.json() == {"appended": 1}
                assert client.post(base + "/voice-entries", json=entries).json() == {"appended": 0}
                assert client.get(base + "/memory").status_code == 200
        finally:
            server.should_exit = True
            worker.join(timeout=TASK_SETTLE_TIMEOUT)
        assert not worker.is_alive()
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
