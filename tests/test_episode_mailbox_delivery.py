from __future__ import annotations

from datetime import datetime, timedelta

from rcp.runs.episode_mailbox import (
    reconcile_chat_mail_recipient,
    reconcile_pending_chat_mail,
)
from rcp.storage import AppStore, AutoResearchMessageRecord
from tests.test_episode_mail_storage import _mail, _mailbox


class _Background:
    def __init__(self, store: AppStore):
        self.store = store
        self.launched: list[str] = []
        self.failure: Exception | None = None
        self.deferred: dict[str, tuple[float, object]] = {}

    def launch_admitted(self, operation_id):
        if self.failure is not None:
            raise self.failure
        self.launched.append(operation_id)
        self.store.mark_agent_task_running(operation_id)
        return self.store.agent_task(operation_id)

    def schedule_deferred_pass(self, key, delay, dispatch):
        self.deferred.setdefault(key, (delay, dispatch))


def _parented(store, root):
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET request_json=json_set(request_json,"
            "'$.started_from_operation_id','origin') WHERE operation_id=?",
            (root.operation_id,),
        )


def _woken(store, operation_id):
    return store.agent_task(operation_id).request["chat_wake"]["item_ids"]


def test_suppressed_backlog_never_hides_a_newer_ending(tmp_path):
    store, episode, root = _mailbox(tmp_path)
    for index in range(70):
        _mail(store, episode, root, f"old-{index:03d}", item_kind="question", source_ref="gone")
    _mail(store, episode, root, "zz-ending", item_kind="ending", source_ref=episode.episode_id)
    background = _Background(store)
    operation_id = reconcile_chat_mail_recipient(background, "project", "chat")
    assert background.launched == [operation_id]
    assert _woken(store, operation_id) == ["zz-ending"]
    assert len(store.pending_episode_mail("chat", "chat")) == 64


def test_global_pass_reaches_a_wakeable_chat_behind_unwakeable_ones(tmp_path, monkeypatch):
    store, episode, root = _mailbox(tmp_path)
    monkeypatch.setattr("rcp.limits.CHAT_MAIL_RECONCILE_LIMIT", 1)
    for chat_id in ("ghost-a", "ghost-b"):
        store.record_episode_message(
            AutoResearchMessageRecord(
                message_id=f"to-{chat_id}",
                episode_id=episode.episode_id,
                sender_role="orchestrator",
                sender_task_id=root.operation_id,
                recipient_kind="chat",
                recipient_chat_id=chat_id,
                body="Result",
                created_at="2000-01-01T00:00:00+00:00",
            )
        )
    _mail(store, episode, root)
    background = _Background(store)
    statuses = reconcile_pending_chat_mail(background)
    assert statuses["ghost-a"] == statuses["ghost-b"] == "chat_wake_binding_unavailable"
    [launched] = background.launched
    assert _woken(store, launched) == ["mail"]


def test_wake_caused_and_parent_caused_items_never_wake(tmp_path):
    store, episode, root = _mailbox(tmp_path)
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET request_json=json_set(request_json,'$.wake_cause','message') "
            "WHERE operation_id=?",
            (root.operation_id,),
        )
    _mail(store, episode, root)
    _mail(store, episode, store.agent_task("origin"), "from-parent")
    background = _Background(store)
    assert reconcile_chat_mail_recipient(background, "project", "chat") == "chat_wake_ineligible"
    assert background.launched == []
    assert store.wake_eligible_chat_mail("project", "chat") == (
        [],
        {"mail": "chat_wake_self_caused", "from-parent": "chat_wake_self_caused"},
    )


def test_a_stop_the_parent_chat_sent_does_not_wake_it(tmp_path):
    store, episode, root = _mailbox(tmp_path)
    _parented(store, root)
    store.request_episode_stop(episode.episode_id, initiated_by="agent:origin")
    store.mark_episode_stop_skipped(episode.episode_id)
    [ending] = store.pending_episode_mail("chat", "chat")
    assert (ending.item_kind, ending.sender_task_id) == ("ending", "origin")
    assert store.wake_eligible_chat_mail("project", "chat")[0] == []


def test_failed_launch_is_kept_once_with_the_parents_child_status(tmp_path):
    store, episode, root = _mailbox(tmp_path)
    _parented(store, root)
    _mail(store, episode, root)
    background = _Background(store)
    background.failure = OSError("ssh: connect to host refused")
    assert reconcile_chat_mail_recipient(background, "project", "chat") == (
        "chat_wake_launch_failed"
    )
    assert reconcile_chat_mail_recipient(background, "project", "chat") == (
        "chat_wake_launch_failed"
    )
    [child] = store.chat_child_episode_status("chat", "project")
    assert child["undelivered_mail"] == [{"code": "chat_wake_launch_failed", "items": 1}]
    background.failure = None
    assert background.launched == [reconcile_chat_mail_recipient(background, "project", "chat")]
    [child] = store.chat_child_episode_status("chat", "project")
    assert "undelivered_mail" not in child


def test_spent_chain_is_reported_as_a_cap(tmp_path, monkeypatch):
    store, episode, root = _mailbox(tmp_path, ceiling=1)
    _parented(store, root)
    _mail(store, episode, root)
    background = _Background(store)
    operation_id = reconcile_chat_mail_recipient(background, "project", "chat")
    assert background.launched == [operation_id]
    store.complete_agent_task(operation_id, applied_revision=None, result={})
    later = (datetime.fromisoformat(store.now()) + timedelta(minutes=1)).isoformat()
    monkeypatch.setattr(store, "now", lambda: later)
    _mail(store, episode, root, "second")
    assert reconcile_chat_mail_recipient(background, "project", "chat") == "chat_wake_ineligible"
    [child] = store.chat_child_episode_status("chat", "project")
    assert child["undelivered_mail"] == [{"code": "chat_wake_chain_limit", "items": 1}]


def test_cooldown_arms_one_later_pass_that_wakes_the_chat(tmp_path, monkeypatch):
    store, episode, root = _mailbox(tmp_path)
    _mail(store, episode, root)
    background = _Background(store)
    reconcile_chat_mail_recipient(background, "project", "chat")
    [operation_id] = background.launched
    store.complete_agent_task(operation_id, applied_revision=None, result={})
    _mail(store, episode, root, "second")
    assert reconcile_chat_mail_recipient(background, "project", "chat") == "chat_wake_cooldown"
    [(delay, later_pass)] = background.deferred.values()
    assert delay > 0
    later = (datetime.fromisoformat(store.now()) + timedelta(minutes=1)).isoformat()
    monkeypatch.setattr(store, "now", lambda: later)
    later_pass()  # type: ignore[operator]
    assert _woken(store, background.launched[-1]) == ["second"]


def test_restart_preserves_an_admitted_wake_before_its_first_dispatch(tmp_path, monkeypatch):
    from rcp.background import BackgroundAgentTasks

    store, episode, root = _mailbox(tmp_path)
    _mail(store, episode, root)
    wake = store.admit_chat_wake("project", "chat", "episode_mail", ["mail"])
    engine = BackgroundAgentTasks(AppStore(store.path), lambda *args: None)
    monkeypatch.setattr(engine, "_schedule_remote_reconciliation", lambda **kwargs: None)
    engine.recover_at_startup()
    assert engine.store.agent_task(wake.operation_id).status == "queued"
    assert [task.operation_id for task in engine.store.pending_chat_wakes()] == [wake.operation_id]
    assert [item.message_id for item in engine.store.claimed_episode_mail(wake.operation_id)] == [
        "mail"
    ]


def test_restart_preserves_and_launches_an_admitted_wake_past_the_first_page(tmp_path, monkeypatch):
    from rcp.background import BackgroundAgentTasks

    monkeypatch.setattr("rcp.limits.CHAT_MAIL_RECONCILE_LIMIT", 1)
    store, episode, root = _mailbox(tmp_path)
    _mail(store, episode, root)
    wake = store.admit_chat_wake("project", "chat", "episode_mail", ["mail"])
    with store.connection() as connection:
        connection.execute(
            "CREATE TEMP TABLE older AS SELECT * FROM graph_runs WHERE operation_id=?",
            (wake.operation_id,),
        )
        connection.execute(
            "UPDATE older SET operation_id='older',created_at='2000-01-01T00:00:00+00:00',"
            "native_session_id=NULL,request_json=json_set(request_json,'$.chat_id','elsewhere')"
        )
        connection.execute("INSERT INTO graph_runs SELECT * FROM older")
    engine = BackgroundAgentTasks(AppStore(store.path), lambda *args: None)
    monkeypatch.setattr(engine, "_schedule_remote_reconciliation", lambda **kwargs: None)
    engine.recover_at_startup()
    assert engine.store.agent_task(wake.operation_id).status == "queued"
    background = _Background(engine.store)
    reconcile_pending_chat_mail(background)
    assert wake.operation_id in background.launched


def test_a_withdrawn_child_question_never_wakes_the_parent(tmp_path):
    from tests.test_api_questions import _question

    store, episode, root = _mailbox(tmp_path)
    question = _question(
        store, "project", owner_kind="episode", owner_id=episode.episode_id, key="seed"
    )
    with store.connection() as connection:
        connection.execute(
            "UPDATE questions SET withdrawn_readonly=1 WHERE question_id=?",
            (question.question_id,),
        )
    _mail(store, episode, root, "withdrawn", item_kind="question", source_ref=question.question_id)
    background = _Background(store)
    reconcile_chat_mail_recipient(background, "project", "chat")
    assert background.launched == []
