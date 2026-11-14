from __future__ import annotations

from unittest.mock import Mock

import pytest

from rcp.notifications import NotificationSender
from rcp.projects import ProjectCatalog
from rcp.server_ops.maintenance import RuntimeAdmissionGate
from rcp.storage.artifact_models import Artifact

from .test_notification_storage import _setup


@pytest.mark.parametrize("withdraw", ["resolve", "remove_mention", "unreadable"])
def test_comment_notification_delivery_and_withdrawal(tmp_path, withdraw, monkeypatch):
    store, device = _setup(tmp_path)
    owner = store.local_owner
    assert owner is not None
    user_id = owner.user_id
    artifact = store.create_artifact(
        Artifact(
            artifact_id="a",
            project_id="p",
            supplier="turn",
            supplier_id="turn",
            source_name="a.txt",
            media_type="text/plain",
            created_at=store.now(),
        ),
        data=b"note",
    )
    thread = store.create_comment_thread(
        "p",
        target_kind="artifact",
        target_id=artifact.artifact_id,
        anchor={"kind": "whole"},
        text=f"<@member:{user_id}>",
        user_id=user_id,
    )
    with store.connection() as connection:
        connection.execute("UPDATE comment_threads SET graph_target='branch:topic'")
    catalog = Mock(spec=ProjectCatalog)
    catalog.open.side_effect = OSError("offline")
    sender = NotificationSender(store, catalog, admission=RuntimeAdmissionGate())
    sender.run_pass()
    pending = sender.pending_desktop(device["device_id"])
    assert len(pending) == 1
    assert pending[0]["reason"] == "comment_mention"
    assert pending[0]["deep_link"] == (
        f"#/projects/p/targets/branch%3Atopic/comment/{thread['thread_id']}"
    )
    store.add_comment_post("p", thread["thread_id"], user_id=user_id, text=f"<@member:{user_id}>")
    sender.run_pass()
    assert len(store.notification_outbox()) == 1
    if withdraw == "resolve":
        store.resolve_comment_thread("p", thread["thread_id"], user_id=user_id)
    elif withdraw == "remove_mention":
        for post in store.get_comment_thread("p", thread["thread_id"])["posts"]:
            store.edit_comment_post("p", post["post_id"], user_id=user_id, text="removed")
    else:
        with store.connection() as connection:
            connection.execute("DELETE FROM project_members WHERE project_id='p'")
    next_attempt = store.notification_outbox()[0]["next_attempt_at"]
    monkeypatch.setattr(store, "now", lambda: next_attempt)
    assert sender.pending_desktop(device["device_id"]) == []
    assert store.notification_outbox() == []
    if withdraw == "resolve":
        store.add_comment_post(
            "p", thread["thread_id"], user_id=user_id, text=f"Again <@member:{user_id}>"
        )
        sender.run_pass()
        assert sender.pending_desktop(device["device_id"]) == []
        assert store.notification_outbox() == []
