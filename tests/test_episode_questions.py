from __future__ import annotations

import pytest

from rcp.storage import AppStore
from rcp.storage.question_models import QuestionOrigin

from .test_episode_storage import _authorizer, _episode, _operational_task, _wrapup


def _question(store, episode, *, key="question", state="pending"):
    question = store.create_or_get_question(
        origin=QuestionOrigin(
            project_id=episode.project_id,
            owner_kind="episode",
            owner_id=episode.episode_id,
            operation_id=f"{episode.episode_id}-turn",
            provider="codex",
            native_session_id="session",
            stage_root="/stage",
            capability="auto_research_orchestrator",
            write_scope_fingerprint="scope",
            graph_target=episode.graph_target,
        ),
        key=key,
        question="Which approach?",
    )
    if state == "answered":
        return store.answer_question(
            question.question_id, answer="First", resolved_by=_authorizer(store)
        )
    if state == "dismissed":
        return store.dismiss_question(question.question_id, resolved_by=_authorizer(store))
    return question


@pytest.mark.parametrize("path", ["fence", "wrapup", "restore", "stop_skipped"])
def test_questions_withdraw_at_ending_fence_before_report_finishes(tmp_path, path):
    store = AppStore(tmp_path / "app.sqlite3")
    episode = store.create_episode(_episode(store, "episode"))
    question = _question(store, episode)
    if path == "fence":
        store.fence_episode_ending("episode", "exhausted")
    elif path == "wrapup":
        store.allocate_episode_invocation(
            "episode", _operational_task(store, "operation", episode_id="episode")
        )
        wrapup, task = _wrapup(store, "episode", "operation", "report")
        store.begin_episode_wrapup("episode", wrapup, task)
    elif path == "stop_skipped":
        store.mark_episode_stop_skipped("episode")
    else:
        with store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            store.detach_experiment_episodes_for_restore(
                connection, diagnostic="Restored copy", now=store.now()
            )
    assert store.get_question(question.question_id).withdrawn_readonly


def test_continuations_reopen_ancestor_questions_without_changing_origins(tmp_path):
    store = AppStore(tmp_path / "app.sqlite3")
    episode = store.create_episode(_episode(store, "original", mode="auto_research"))
    questions = [
        _question(store, episode, key=state, state=state)
        for state in ("pending", "answered", "dismissed")
    ]
    store.end_episode_without_report("original", ending="exhausted", diagnostic=None)
    for episode_id in ("second", "third"):
        continuation = _episode(store, episode_id, mode="auto_research").model_copy(
            update={"continues_episode_id": episode.episode_id}
        )
        episode = store.create_episode(continuation)
        carried = store.episode_questions(episode.project_id, episode.episode_id)
        assert {q.question_id for q in carried} == {q.question_id for q in questions}
        for original in questions:
            current = store.get_question(original.question_id)
            assert current.origin == original.origin
            assert current.state == original.state
            assert not current.withdrawn_readonly
        snapshots = store.auto_research_space_run_projection_snapshots(
            {episode.project_id}, completed_since=store.now()
        )
        current = next(s for s in snapshots if s.episode.episode_id == episode_id)
        assert current.has_open_questions
        store.end_episode_without_report(episode_id, ending="exhausted", diagnostic=None)
        assert all(q.withdrawn_readonly for q in store.episode_questions("project", episode_id))
    assert store.episode_questions("other-project", "third") == []


@pytest.mark.parametrize("delivered", [False, True])
def test_only_undelivered_question_mail_moves_to_latest_continuation(tmp_path, delivered):
    from rcp.storage import (
        AutoResearchLifecycleNoticeRecord,
        AutoResearchMessageRecord,
        AutoResearchStateRecord,
    )

    from .test_auto_research_commands import _auto_research_authority, _setup_auto_research

    store, original_episode, root = _setup_auto_research(tmp_path)
    # Continuation admission requires the orchestrator's saved session and stage.
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET native_session_id = 'session', stage_root = '/stage' "
            "WHERE operation_id = ?",
            (root.operation_id,),
        )
    question = _question(store, original_episode, state="answered")
    message = store.record_auto_research_message(
        AutoResearchMessageRecord(
            message_id=f"question:{question.question_id}:answer:{question.answer_revision}",
            episode_id=original_episode.episode_id,
            sender_role="human",
            authorized_by=question.resolved_by,
            recipient_task_id=root.operation_id,
            body="Answer to the question",
            created_at=store.now(),
        )
    )
    ordinary = store.record_auto_research_message(
        message.model_copy(update={"message_id": "ordinary-mail"})
    )
    if delivered:
        store.mark_auto_research_messages_delivered(
            [message.message_id], operation_id=root.operation_id
        )
    episode = original_episode
    for episode_id in ("continuation", "latest"):
        store.end_episode_without_report(episode.episode_id, ending="exhausted", diagnostic=None)
        continuation = _episode(store, episode_id, mode="auto_research").model_copy(
            update={
                "graph_target": original_episode.graph_target,
                "graph_base_head": original_episode.graph_base_head,
                "continues_episode_id": episode.episode_id,
                "continuation_request_id": f"request-{episode_id}",
                "authorized_by": original_episode.authorized_by,
            }
        )
        task = root.model_copy(
            update={
                "episode_id": episode_id,
                "operation_id": f"root-{episode_id}",
                "status": "queued",
                "native_session_id": "session",
                "stage_root": "/stage",
                "request": {
                    **root.request,
                    "episode_id": episode_id,
                    "actor_operation_id": f"root-{episode_id}",
                },
                "dispatch_authority": _auto_research_authority(episode_id, "orchestrator"),
            }
        )
        episode, task, replayed = store.create_auto_research_continuation(
            continuation,
            AutoResearchStateRecord(
                episode_id=episode_id, created_at=store.now(), updated_at=store.now()
            ),
            task,
            AutoResearchLifecycleNoticeRecord(
                notice_id=f"notice-{episode_id}",
                episode_id=episode_id,
                source_kind="episode",
                source_id=continuation.continues_episode_id,
                source_event="reauthorized",
                payload={},
                created_at=store.now(),
            ),
        )
        assert not replayed
        store.complete_agent_task(task.operation_id, applied_revision=None, result={})
    # A late idempotent ending replay must not withdraw reopened ancestor cards.
    store.end_episode_without_report(
        original_episode.episode_id, ending="exhausted", diagnostic=None
    )
    assert not store.get_question(question.question_id).withdrawn_readonly
    moved = store.readdress_auto_research_question_answer(question.question_id)
    assert moved.episode_id == (original_episode.episode_id if delivered else episode.episode_id)
    assert moved.recipient_task_id == (root.operation_id if delivered else task.operation_id)
    assert moved.authorized_by == question.resolved_by
    assert store.readdress_auto_research_question_answer(question.question_id) == moved
    assert store.auto_research_message(ordinary.message_id) == ordinary
    assert store.get_question(question.question_id).origin == question.origin
    assert store.episode_budget_meter(episode.episode_id).invocations_used == 1
