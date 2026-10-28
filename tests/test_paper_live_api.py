"""Exercise real Paper REST and websocket transports on disposable databases."""

from __future__ import annotations

import pytest
from pycrdt import Doc, Text, create_update_message, handle_sync_message
from starlette.websockets import WebSocketDisconnect

from tests.helpers import create_named_app, signed_in_client
from tests.test_project_membership import _create_project, _team_app


def _file(client, prefix, path="chapter.md"):
    response = client.post(prefix + "/files", json={"path": path, "kind": "text"})
    assert response.status_code == 200, response.text
    return response.json()["file_ids"][0]


def _sync(socket, doc):
    message = socket.receive_bytes()
    assert message[0] == 0
    reply = handle_sync_message(message[1:], doc)
    if reply is not None:
        socket.send_bytes(reply)
    _ack(socket, doc)


def _ack(socket, doc):
    for _ in range(12):
        message = socket.receive_bytes()
        if message[0] == 100:
            return message
        if message[0] == 0:
            reply = handle_sync_message(message[1:], doc)
            if reply is not None:
                socket.send_bytes(reply)
    pytest.fail("No durable acknowledgement received")


def test_two_websocket_clients_converge_and_ack_is_committed(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    with signed_in_client(app) as client:
        prefix = f"/api/projects/{app.state.default_project_id}/paper"
        file_id = _file(client, prefix)
        url = prefix + f"/files/{file_id}/live"
        left, right = Doc({"text": Text()}), Doc({"text": Text()})
        with (
            client.websocket_connect(url, headers={"Origin": "http://testserver"}) as one,
            client.websocket_connect(url, headers={"Origin": "http://testserver"}) as two,
        ):
            _sync(one, left)
            _sync(two, right)
            left["text"].insert(0, "left")
            right["text"].insert(0, "right")
            one.send_bytes(create_update_message(left.get_update()))
            two.send_bytes(create_update_message(right.get_update()))
            _ack(one, left)
            _ack(two, right)
            # Request a new state sync after both durable acknowledgements.
            from pycrdt import create_sync_message

            one.send_bytes(create_sync_message(left))
            two.send_bytes(create_sync_message(right))
            _ack(one, left)
            _ack(two, right)
            assert str(left["text"]) == str(right["text"])
            saved = client.get(prefix + f"/files/{file_id}/raw")
            assert saved.text == str(left["text"])
            with app.state.services.store.connection() as connection:
                row = connection.execute(
                    "SELECT saved_revision FROM paper_publication WHERE project_id = ?",
                    (app.state.default_project_id,),
                ).fetchone()
                assert row[0] >= 3


def test_tree_upload_history_restore_and_raw_content_policy(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    with signed_in_client(app) as client:
        prefix = f"/api/projects/{app.state.default_project_id}/paper"
        file_id = _file(client, prefix)
        version = client.post(prefix + "/history", json={"name": "Before move"})
        assert version.status_code == 200, version.text
        version_id = version.json()["version_id"]
        assert (
            client.post(prefix + f"/files/{file_id}/rename", json={"path": "moved.typ"}).status_code
            == 200
        )
        assert client.delete(prefix + f"/files/{file_id}").status_code == 200
        restored = client.post(
            prefix + f"/history/{version_id}/restore", json={"file_ids": [file_id]}
        )
        assert restored.status_code == 200, restored.text
        assert client.get(prefix + "/tree").json()["files"][0]["path"] == "chapter.md"
        for path, data, content_type, disposition in (
            ("figure.png", b"raster", "image/png", "inline"),
            ("figure.svg", b"<svg onload='alert(1)'/>", "image/svg+xml", "attachment"),
            ("page.html", b"<script>alert(1)</script>", "text/html", "attachment"),
        ):
            response = client.post(
                prefix + "/uploads", data={"path": path}, files={"file": (path, data, content_type)}
            )
            assert response.status_code == 200, response.text
            binary_id = response.json()["file_ids"][0]
            raw = client.get(prefix + f"/files/{binary_id}/raw")
            assert raw.content == data
            assert raw.headers["x-content-type-options"] == "nosniff"
            assert raw.headers["content-disposition"].startswith(disposition)
            if path.endswith(".png"):
                replaced = client.put(
                    prefix + f"/files/{binary_id}/content",
                    files={"file": (path, b"changed", content_type)},
                )
                assert replaced.status_code == 200, replaced.text
                assert client.get(prefix + f"/files/{binary_id}/raw").content == b"changed"
        text_id = next(
            file["file_id"]
            for file in client.get(prefix + "/tree").json()["files"]
            if file["path"] == "chapter.md"
        )
        uploaded = client.put(
            prefix + f"/files/{text_id}/content",
            files={"file": ("chapter.md", b"# Uploaded\n", "text/markdown")},
        )
        assert uploaded.status_code == 200, uploaded.text
        assert client.get(prefix + f"/files/{text_id}/raw").content == b"# Uploaded\n"
        assert client.get(prefix + "/sessions").status_code == 200


def test_websocket_membership_and_revocation(tmp_path, monkeypatch):
    monkeypatch.setattr("rcp.api.paper_live.PAPER_ADMISSION_RECHECK_SECONDS", 0.01)
    app, client, store, people, acting = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "repo")
    prefix = f"/api/projects/{project_id}/paper"
    with client:
        file_id = _file(client, prefix)
        url = prefix + f"/files/{file_id}/live"
        acting[0] = people[1].user_id
        assert client.get(prefix + "/tree").status_code == 404
        with (
            pytest.raises(WebSocketDisconnect) as refused,
            client.websocket_connect(url, headers={"Origin": "http://testserver"}) as socket,
        ):
            socket.receive_bytes()
        assert refused.value.code == 4403
        acting[0] = people[0].user_id
        with client.websocket_connect(url, headers={"Origin": "http://testserver"}) as socket:
            _sync(socket, Doc({"text": Text()}))
            with store.connection() as connection:
                connection.execute(
                    "DELETE FROM project_members WHERE project_id = ? AND user_id = ?",
                    (project_id, people[0].user_id),
                )
            with pytest.raises(WebSocketDisconnect) as revoked:
                socket.receive_bytes()
            assert revoked.value.code == 4403


def test_upload_bounds_apply_before_file_spooling(manifest, tmp_path, monkeypatch):
    monkeypatch.setattr("rcp.api.paper.PAPER_MAX_UPLOAD_BYTES", 8)
    monkeypatch.setattr("rcp.api.paper.PAPER_MULTIPART_OVERHEAD_BYTES", 512)
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    with signed_in_client(app) as client:
        prefix = f"/api/projects/{app.state.default_project_id}/paper"
        response = client.post(
            prefix + "/uploads",
            data={"path": "large.bin"},
            files={"file": ("large.bin", b"x" * 1024)},
        )
        assert response.status_code == 413, response.text
        assert client.get(prefix + "/tree").json()["files"] == []


def test_presence_replays_to_joining_clients_and_clears_on_disconnect(manifest, tmp_path):
    from pycrdt import Awareness, Decoder, create_awareness_message

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    with signed_in_client(app) as client:
        prefix = f"/api/projects/{app.state.default_project_id}/paper"
        file_id = _file(client, prefix)
        url = prefix + f"/files/{file_id}/live"
        doc = Doc({"text": Text()})
        presence = Awareness(doc)
        presence.set_local_state({"name": "Writer"})
        observer = Awareness(Doc())
        with client.websocket_connect(url, headers={"Origin": "http://testserver"}) as one:
            _sync(one, doc)
            one.send_bytes(
                create_awareness_message(presence.encode_awareness_update([doc.client_id]))
            )
            assert one.receive_bytes()[0] == 1
            with client.websocket_connect(url, headers={"Origin": "http://testserver"}) as two:
                assert two.receive_bytes()[0] == 0
                replay = two.receive_bytes()
                assert replay[0] == 1
                observer.apply_awareness_update(Decoder(replay[1:]).read_message(), None)
                assert observer.states[doc.client_id] == {"name": "Writer"}
                one.close()
                removed = two.receive_bytes()
                assert removed[0] == 1
                observer.apply_awareness_update(Decoder(removed[1:]).read_message(), None)
                assert doc.client_id not in observer.states


def test_served_live_edit_publish_rename_and_shutdown(manifest, tmp_path, monkeypatch, caplog):
    import logging
    import socket
    import threading
    from types import SimpleNamespace

    import httpx
    import uvicorn
    from websockets.sync.client import connect

    from rcp import limits
    from tests.helpers import TASK_SETTLE_TIMEOUT, wait_until

    monkeypatch.setattr(limits, "PAPER_TIMER_SECONDS", 0.05)
    monkeypatch.setattr(limits, "PAPER_PUBLISH_IDLE_SECONDS", 0)
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.services.store
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        assert port != 8421
        server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=True))
        worker = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        worker.start()
        try:
            wait_until(lambda: server.started, timeout=TASK_SETTLE_TIMEOUT)
            origin = f"http://127.0.0.1:{port}"
            with httpx.Client(base_url=origin, trust_env=False) as client:
                assert (
                    client.post(
                        "/api/owner/redeem", json={"code": store.create_owner_sign_in_code()}
                    ).status_code
                    == 200
                )
                prefix = f"/api/projects/{app.state.default_project_id}/paper"
                file_id = _file(client, prefix)
                cookie = "; ".join(f"{key}={value}" for key, value in client.cookies.items())
                uri = f"ws://127.0.0.1:{port}{prefix}/files/{file_id}/live"
                left, right = Doc({"text": Text()}), Doc({"text": Text()})
                with (
                    connect(uri, origin=origin, additional_headers={"Cookie": cookie}) as first,
                    connect(uri, origin=origin, additional_headers={"Cookie": cookie}) as second,
                ):
                    one = SimpleNamespace(
                        receive_bytes=lambda: first.recv(timeout=5), send_bytes=first.send
                    )
                    two = SimpleNamespace(
                        receive_bytes=lambda: second.recv(timeout=5), send_bytes=second.send
                    )
                    _sync(one, left)
                    _sync(two, right)
                    left["text"].insert(0, "Saved over a network socket")
                    one.send_bytes(create_update_message(left.get_update()))
                    _ack(one, left)
                    from pycrdt import create_sync_message

                    two.send_bytes(create_sync_message(right))
                    _ack(two, right)
                    assert str(right["text"]) == str(left["text"])
                assert (
                    client.post(
                        prefix + f"/files/{file_id}/rename", json={"path": "renamed.md"}
                    ).status_code
                    == 200
                )
                wait_until(
                    lambda: client.get(prefix + "/tree").json()["publish_state"] == "published",
                    timeout=TASK_SETTLE_TIMEOUT,
                )
                assert (manifest.research_dir / "paper/renamed.md").read_text() == str(left["text"])
                assert not (manifest.research_dir / "paper/chapter.md").exists()
        finally:
            server.should_exit = True
            worker.join(timeout=TASK_SETTLE_TIMEOUT)
        assert not worker.is_alive()
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


@pytest.mark.parametrize("failure", ["nul", "large", "invalid", "project_rate", "socket_rate"])
def test_rejected_updates_close_finally_but_rate_limits_retry(
    manifest, tmp_path, monkeypatch, failure
):
    from rcp import limits

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    with signed_in_client(app) as client:
        prefix = f"/api/projects/{app.state.default_project_id}/paper"
        file_id = _file(client, prefix)
        with client.websocket_connect(
            prefix + f"/files/{file_id}/live", headers={"Origin": "http://testserver"}
        ) as socket:
            doc = Doc({"text": Text()})
            _sync(socket, doc)
            if failure == "large":
                monkeypatch.setattr(limits, "PAPER_MAX_DOC_BYTES", 2)
            if failure == "project_rate":
                monkeypatch.setattr(limits, "PAPER_PROJECT_UPDATES_PER_SECOND", 0)
            if failure == "socket_rate":
                monkeypatch.setattr("rcp.api.paper_live.PAPER_CONNECTION_UPDATES_PER_SECOND", 0)
            doc["text"].insert(0, "\x00" if failure == "nul" else "large")
            socket.send_bytes(
                b"invalid" if failure == "invalid" else create_update_message(doc.get_update())
            )
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_bytes()
            assert closed.value.code == (
                4529 if failure.endswith("rate") else 4413 if failure == "large" else 4400
            )


@pytest.mark.parametrize("refusal,code", [("paused", 4503), ("admission", 4503), ("deleted", 4403)])
def test_refused_socket_accepts_then_closes_with_recovery_code(
    manifest, tmp_path, monkeypatch, refusal, code
):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    with signed_in_client(app) as client:
        project = app.state.default_project_id
        prefix = f"/api/projects/{project}/paper"
        file_id = _file(client, prefix)
        if refusal == "paused":
            app.state.paper_live.run_sync(app.state.paper_live.pause(project))
        elif refusal == "admission":

            def refuse(*args):
                raise ValueError("closed")

            monkeypatch.setattr(
                app.state.services.store, "require_project_accepts_new_work", refuse
            )
        else:
            assert client.delete(prefix + f"/files/{file_id}").status_code == 200
        with client.websocket_connect(
            prefix + f"/files/{file_id}/live", headers={"Origin": "http://testserver"}
        ) as socket:
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_bytes()
            assert closed.value.code == code


def test_delete_named_version_releases_quota_and_refuses_auto(manifest, tmp_path, monkeypatch):
    from rcp import limits

    monkeypatch.setattr(limits, "PAPER_HISTORY_NAMED_LIMIT", 1)
    monkeypatch.setattr(limits, "PAPER_HISTORY_IDLE_SECONDS", 0)
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    with signed_in_client(app) as client:
        prefix = f"/api/projects/{app.state.default_project_id}/paper"
        _file(client, prefix)
        live = app.state.paper_live
        live.run_sync(live.tick())
        auto = client.get(prefix + "/history").json()[0]["version_id"]
        assert client.delete(prefix + f"/history/{auto}").status_code == 409
        version = client.post(prefix + "/history", json={"name": "kept"}).json()["version_id"]
        assert client.post(prefix + "/history", json={"name": "full"}).status_code == 409
        assert client.delete(prefix + f"/history/{version}").status_code == 204
        assert version not in {row["version_id"] for row in client.get(prefix + "/history").json()}
        assert client.post(prefix + "/history", json={"name": "available"}).status_code == 200


def test_delete_checkpoints_unversioned_peer_text_and_history_names(tmp_path):
    from rcp import limits

    app, client, _, people, _ = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "repo")
    prefix = f"/api/projects/{project_id}/paper"
    with client:
        file_id = _file(client, prefix)
        assert client.get(prefix + "/tree").json()["max_doc_bytes"] == limits.PAPER_MAX_DOC_BYTES
        named = client.post(prefix + "/history", json={"name": "before"}).json()
        with client.websocket_connect(
            prefix + f"/files/{file_id}/live", headers={"Origin": "http://testserver"}
        ) as socket:
            peer = Doc({"text": Text()})
            _sync(socket, peer)
            peer["text"].insert(0, "Recent peer text")
            socket.send_bytes(create_update_message(peer.get_update()))
            _ack(socket, peer)
        assert client.delete(prefix + f"/files/{file_id}").status_code == 200
        history = client.get(prefix + "/history").json()
        assert (
            next(row for row in history if row["version_id"] == named["version_id"])["actor_name"]
            == people[0].display_name
        )
        automatic = next(row for row in history if row["actor"] == "automatic")
        assert automatic["actor_name"] == "automatic"
        assert (
            client.get(prefix + f"/history/{automatic['version_id']}/files/{file_id}").text
            == "Recent peer text"
        )
        restored = client.post(
            prefix + f"/history/{automatic['version_id']}/restore", json={"file_ids": [file_id]}
        )
        assert restored.status_code == 200, restored.text
        restored_id = restored.json()["file_ids"][0]
        assert client.get(prefix + f"/files/{restored_id}/raw").text == "Recent peer text"
