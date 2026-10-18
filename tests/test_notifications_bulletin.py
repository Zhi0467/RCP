from __future__ import annotations

from types import SimpleNamespace

from rcp.notifications import NotificationSender
from rcp.server_ops.maintenance import RuntimeAdmissionGate
from rcp.storage import AppStore

from .test_episode_api_serialization import _project


def test_mentions_post_edit_recipient_only_and_restart_dedup(tmp_path):
    store, bootstrap = AppStore.initialize_team_space(tmp_path / "rcp.sqlite3", "Team")
    author, author_token = store.enroll_team_member(bootstrap, "Author")
    members = [(author, author_token)]
    for name in ("First", "Second", "Unmentioned"):
        _, code = store.create_team_invitation(author.user_id)
        members.append(store.enroll_team_member(code, name))
    _project(store)
    devices = {}
    for member, token in members:
        store.seat_project_member("project", member.user_id)
        store.create_team_session(token)
        session = store.team_sessions(member.user_id)[0]
        devices[member.user_id] = store.register_notification_device(
            member.user_id, session_id=session.session_id
        )["device_id"]
    first, second = members[1][0], members[2][0]
    post = store.add_bulletin_post(
        "project",
        "main",
        f"<@member:{author.user_id}> <@member:{first.user_id}> <@member:{first.user_id}>",
        user_id=author.user_id,
        key="post",
    )
    catalog = SimpleNamespace(open=lambda _: (_ for _ in ()).throw(OSError("offline")))
    sender = NotificationSender(store, catalog, admission=RuntimeAdmissionGate())
    sender.run_pass()
    rows = store.notification_outbox()
    assert [(row["device_id"], row["kind"], row["item_id"]) for row in rows] == [
        (devices[first.user_id], "bulletin_mention", post["post_id"])
    ]
    assert rows[0]["deep_link"] == (
        f"#/projects/project?view=chats&channel=main&post={post['post_id']}"
    )
    assert sender.pending_desktop(devices[first.user_id])[0]["reason"] == "bulletin_mention"
    store.mutate_bulletin_post(
        "project",
        "main",
        post["post_id"],
        text=f"<@member:{first.user_id}> <@member:{second.user_id}>",
        user_id=author.user_id,
        key="edit",
    )
    # Removing the mention before a pass must not erase its accepted event.
    store.mutate_bulletin_post(
        "project",
        "main",
        post["post_id"],
        text="removed mentions",
        user_id=author.user_id,
        key="remove",
    )
    restarted = NotificationSender(AppStore(store.path), catalog, admission=RuntimeAdmissionGate())
    restarted.run_pass()
    restarted.run_pass()
    rows = store.notification_outbox()
    assert {row["device_id"] for row in rows} == {devices[first.user_id], devices[second.user_id]}
    assert len(rows) == 2
    # Outbox expiry/deletion cannot resurrect already consumed mentions either.
    for row in rows:
        store.drop_notification(row["device_id"], row["notification_id"])
    restarted.run_pass()
    assert store.notification_outbox() == []
