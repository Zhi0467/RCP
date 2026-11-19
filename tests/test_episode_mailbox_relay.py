"""A Work chat's child episodes write to it, and the chat answers them like the human."""

from __future__ import annotations

from pathlib import Path

import pytest

from rcp.machine_cards import BACKEND_MACHINE_ID
from rcp.storage import AutoResearchMessageRecord, EpisodeNotRunning, EpisodeRecord
from rcp.storage.question_models import QuestionOrigin

from .helpers import signed_in_client, wait_for_task, wait_until
from .test_work_episode_controls import KICKOFF, THREAD_CHAT, _Member


def _orchestrator_question(member: _Member, episode_id: str, root_id: str, key: str):
    root = member.store.agent_task(root_id)
    assert root is not None
    return member.store.create_or_get_question(
        origin=QuestionOrigin(
            owner_kind="episode",
            project_id=member.project_id,
            owner_id=episode_id,
            operation_id=root_id,
            provider="codex",
            native_session_id="orchestrator-session",
            stage_root=str(member.stage),
            stage_machine_id=BACKEND_MACHINE_ID,
            capability="orchestrate",
            write_scope_fingerprint="a" * 64,
            graph_target=root.graph_target,
        ),
        key=key,
        question="Which dataset?",
    )


def _started(member: _Member) -> str:
    kickoff = member.turn("kickoff-turn")
    assert kickoff("go", KICKOFF).result["disposition"] == "card_pending"
    tapped = member.start(member.land("kickoff-turn"))
    assert tapped.status_code == 200, tapped.text
    return str(tapped.json()["result"]["episode_id"])


def test_orchestrator_question_relays_once_and_the_first_answer_wins(
    manifest, tmp_path: Path, monkeypatch
) -> None:
    member = _Member(manifest, tmp_path, monkeypatch)
    store = member.store
    member.provider_ready()
    member.settling_orchestrator()
    episode_id = _started(member)
    act = member.turn("answer-turn")
    root_id = store.episode(episode_id).root_operation_id  # type: ignore[union-attr]
    assert root_id is not None
    wait_for_task(store, root_id, expect="succeeded")

    question = _orchestrator_question(member, episode_id, root_id, "dataset")
    replayed = _orchestrator_question(member, episode_id, root_id, "dataset")
    assert replayed.question_id == question.question_id
    [item] = store.pending_episode_mail("chat", THREAD_CHAT)
    assert (item.item_kind, item.source_ref, item.recipient_task_id) == (
        "question",
        question.question_id,
        None,
    )
    [child] = store.chat_child_episode_status(THREAD_CHAT, member.project_id)
    assert child["episode_id"] == episode_id
    assert [open_["question_id"] for open_ in child["open_questions"]] == [  # type: ignore[union-attr]
        question.question_id
    ]

    answered = act(
        "answer",
        {"action": "answer", "question_id": question.question_id, "answer_file": "note.md"},
    )
    assert answered.status == "ok", answered.message
    assert store.get_question(question.question_id).state == "answered"  # type: ignore[union-attr]
    again = act(
        "answer",
        {"action": "answer", "question_id": question.question_id, "answer_file": "note.md"},
    )
    assert again.status == "ok", again.message

    human = signed_in_client(member.app).post(
        f"/api/projects/{member.project_id}/questions/{question.question_id}/answer",
        json={"answer": "A different dataset"},
    )
    assert human.status_code == 409, human.text
    assert human.json()["detail"]["code"] == "question_already_answered"
    late = act(
        "late", {"action": "answer", "question_id": question.question_id, "answer_file": "goal.md"}
    )
    assert (late.status, late.result["code"]) == ("invalid", "question_already_answered")


def test_stopped_orchestrator_ending_reaches_its_chat_and_the_parent_cannot_spend_turns(
    manifest, tmp_path: Path, monkeypatch
) -> None:
    member = _Member(manifest, tmp_path, monkeypatch)
    store = member.store
    member.provider_ready()
    member.settling_orchestrator()
    episode_id = _started(member)
    act = member.turn("control-turn")
    root_id = store.episode(episode_id).root_operation_id  # type: ignore[union-attr]
    assert root_id is not None
    wait_for_task(store, root_id, expect="succeeded")
    stopped = act("stop", {"action": "stop", "episode_id": episode_id})
    assert stopped.status == "ok", stopped.message
    wait_until(lambda: store.episode(episode_id).status == "stopped")  # type: ignore[union-attr]

    endings = [
        item
        for item in store.pending_episode_mail("chat", THREAD_CHAT)
        if item.item_kind == "ending"
    ]
    assert [(item.source_ref, item.sender_role) for item in endings] == [
        (episode_id, "orchestrator")
    ]
    [child] = store.chat_child_episode_status(THREAD_CHAT, member.project_id)
    assert (child["status"], child["open_questions"]) == ("stopped", [])


def test_experiment_mail_waits_for_the_next_invocation_and_an_ended_chain_refuses(
    manifest, tmp_path: Path, monkeypatch
) -> None:
    member = _Member(manifest, tmp_path, monkeypatch)
    store = member.store
    now = store.now()
    episode = store.create_episode(
        EpisodeRecord(
            episode_id="experiment",
            project_id=member.project_id,
            mode="experiment_loop",
            control_node_id="node-1",
            status="queued",
            invocation_ceiling=2,
            authorized_by=member.human,
            created_at=now,
            updated_at=now,
        )
    )

    def mail(message_id: str) -> AutoResearchMessageRecord:
        return store.record_episode_message(
            AutoResearchMessageRecord(
                message_id=message_id,
                episode_id=episode.episode_id,
                sender_role="human",
                authorized_by=member.human,
                recipient_kind="episode",
                body="Use the smaller batch.",
                created_at=store.now(),
            )
        )

    sent = mail("steer")
    assert sent.delivered_at is None and sent.recipient_task_id is None
    [claimed] = store.claim_episode_mail("episode", episode.episode_id, ["invocation-2"])
    assert claimed.delivery_operation_id == "invocation-2"
    assert store.claim_episode_mail("episode", episode.episode_id, ["invocation-3"]) == []
    [again] = store.claim_episode_mail(
        "episode", episode.episode_id, ["invocation-2-retry", "invocation-2"]
    )
    assert again.message_id == "steer"

    store.end_episode_without_report(episode.episode_id, ending="failed")
    with pytest.raises(EpisodeNotRunning):
        mail("late")
    assert store.auto_research_message("late") is None
