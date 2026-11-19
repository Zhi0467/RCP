"""A Work chat's child episodes write to it, and the chat answers them like the human."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from rcp.agents.command_mailbox import CommandTurnIdentity
from rcp.agents.command_protocol import AskCommandRequest
from rcp.machine_cards import BACKEND_MACHINE_ID
from rcp.runs.questions import work_command_handler
from rcp.storage import AutoResearchMessageRecord, EpisodeNotRunning, EpisodeRecord
from rcp.storage.question_models import QuestionOrigin
from rcp.transport.workspace_mailbox import RunStageMailbox

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
    stale = _orchestrator_question(member, episode_id, root_id, "stale")
    with store.connection() as connection:
        connection.execute(
            "UPDATE questions SET withdrawn_readonly=1 WHERE question_id=?", (stale.question_id,)
        )
    [child] = store.chat_child_episode_status(THREAD_CHAT, member.project_id)
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


def _settled(member: _Member) -> None:
    workers = list(member.background._workers.values())
    wait_until(lambda: not any(worker.is_alive() for worker in workers))


def _chat_wakes(store, chat_id: str) -> list[dict]:
    with store.connection() as connection:
        return [
            {"operation_id": row[0], "item_ids": json.loads(row[1])}
            for row in connection.execute(
                "SELECT operation_id,json_extract(request_json,'$.chat_wake.item_ids') "
                "FROM graph_runs WHERE json_extract(request_json,'$.chat_id')=? "
                "AND json_extract(request_json,'$.chat_wake') IS NOT NULL",
                (chat_id,),
            )
        ]


def test_a_child_question_wakes_the_idle_parent_chat_once(
    manifest, tmp_path: Path, monkeypatch
) -> None:
    member = _Member(manifest, tmp_path, monkeypatch)
    store = member.store
    member.provider_ready()
    member.settling_orchestrator()
    episode_id = _started(member)
    root_id = store.episode(episode_id).root_operation_id  # type: ignore[union-attr]
    assert root_id is not None
    wait_for_task(store, root_id, expect="succeeded")
    _settled(member)

    question = _orchestrator_question(member, episode_id, root_id, "dataset")
    item_id = f"episode:{episode_id}:question:{question.question_id}"
    wait_until(lambda: _chat_wakes(store, THREAD_CHAT))
    [wake] = _chat_wakes(store, THREAD_CHAT)
    assert wake["item_ids"] == [item_id]
    assert store.pending_episode_mail("chat", THREAD_CHAT) == []
    _orchestrator_question(member, episode_id, root_id, "dataset")
    assert len(_chat_wakes(store, THREAD_CHAT)) == 1


def test_a_child_pause_wakes_the_idle_parent_chat(manifest, tmp_path: Path, monkeypatch) -> None:
    member = _Member(manifest, tmp_path, monkeypatch)
    store = member.store
    member.provider_ready()
    member.settling_orchestrator()
    episode_id = _started(member)
    root_id = store.episode(episode_id).root_operation_id  # type: ignore[union-attr]
    assert root_id is not None
    wait_for_task(store, root_id, expect="succeeded")
    _settled(member)
    assert _chat_wakes(store, THREAD_CHAT) == []

    store.end_episode_without_report(episode_id, ending="human_pause")
    wait_until(lambda: _chat_wakes(store, THREAD_CHAT))
    [wake] = _chat_wakes(store, THREAD_CHAT)
    assert wake["item_ids"] == [f"episode:{episode_id}:pause:{episode_id}"]


def _chat_ask(member: _Member, operation_id: str, key: str, **arguments: object):
    member.turn(operation_id)
    handler = work_command_handler(
        member.executions[operation_id],
        None,
        mailbox=RunStageMailbox(member.workspace, None),
    )
    request = AskCommandRequest.model_validate(
        {
            "mailbox_id": "a" * 32,
            "request_id": uuid.uuid4().hex,
            "credential": "b" * 64,
            "verb": "ask",
            "idempotency_key": key,
            "arguments": arguments,
        }
    )
    identity = CommandTurnIdentity(
        episode_id=None, task_id=operation_id, turn_id="turn", authority="broker"
    )
    response = handler(request, identity)
    member.store.complete_agent_task(operation_id, applied_revision=None, result={"messages": []})
    return response


def test_a_human_answer_to_the_child_withdraws_only_the_parents_linked_relay(
    manifest, tmp_path: Path, monkeypatch
) -> None:
    member = _Member(manifest, tmp_path, monkeypatch)
    store = member.store
    member.provider_ready()
    member.settling_orchestrator()
    episode_id = _started(member)
    root_id = store.episode(episode_id).root_operation_id  # type: ignore[union-attr]
    assert root_id is not None
    wait_for_task(store, root_id, expect="succeeded")
    _settled(member)
    question = _orchestrator_question(member, episode_id, root_id, "dataset")
    wait_until(lambda: _chat_wakes(store, THREAD_CHAT))
    [wake] = _chat_wakes(store, THREAD_CHAT)
    wait_until(
        lambda: (
            store.agent_task(wake["operation_id"]).status  # type: ignore[union-attr]
            not in {"queued", "running"}
        )
    )
    _settled(member)

    relayed = _chat_ask(
        member, "relay-turn", "relay", question="Which dataset?", relays=question.question_id
    )
    assert relayed.status == "ok", relayed.message
    same_wake = store.create_or_get_question(
        origin=question.origin.model_copy(
            update={
                "owner_kind": "chat",
                "owner_id": THREAD_CHAT,
                "operation_id": wake["operation_id"],
            }
        ),
        key="other",
        question="Anything else?",
    )
    stranger = _chat_ask(
        member, "stranger-turn", "relay", question="Which dataset?", relays="missing"
    )
    assert stranger.status == "invalid"

    answered = signed_in_client(member.app).post(
        f"/api/projects/{member.project_id}/questions/{question.question_id}/answer",
        json={"answer": "The small one"},
    )

    assert answered.status_code == 200, answered.text
    relay_record = store.get_question(relayed.result["question_id"])
    other_record = store.get_question(same_wake.question_id)
    assert relay_record is not None and relay_record.withdrawn_readonly
    assert other_record is not None and not other_record.withdrawn_readonly
    late = _chat_ask(
        member, "late-turn", "late", question="Which dataset?", relays=question.question_id
    )
    assert late.status == "invalid"


def test_the_client_sends_a_relay_and_refuses_it_on_an_approval_card(tmp_path: Path) -> None:
    from rcp.agents.command_protocol import AskArguments
    from rcp.agents.staged_command_client import ClientInputError, _parser, _request_arguments

    args = ["--broker", "broker", "--timeout", "1", "--workspace", str(tmp_path), "ask"]
    ask = [*args, "--key", "relay", "--question", "Which?", "--relays", "q1"]
    _verb, _key, arguments = _request_arguments(_parser().parse_args(ask), tmp_path)
    assert AskArguments.model_validate(arguments).relays == "q1"
    with pytest.raises(ClientInputError):
        _request_arguments(_parser().parse_args([*ask, "--action", "{}"]), tmp_path)
