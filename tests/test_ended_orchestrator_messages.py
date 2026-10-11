"""Human mail reauthorizes an ended orchestrator without missing its opening turn."""

from __future__ import annotations

import uuid

import pytest

import rcp.api.episode_routes as routes
from rcp.agents import AgentEvent
from rcp.runs.auto_research import auto_research_exhaustion_signal, auto_research_wrapup_spec
from rcp.runs.auto_research_admission import continue_auto_research
from rcp.runs.episodes.reconcile import EpisodeReconciler
from rcp.runs.episodes.wrapup import begin_episode_report_wrapup
from tests.helpers import create_named_app, signed_in_client, wait_for_task
from tests.test_episode_api import _sse, create_terminal_auto_episode


def _ended_app(manifest, tmp_path, monkeypatch, *, launch=False):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    tasks = app.state.background_tasks
    store = tasks.store
    original, _, _ = create_terminal_auto_episode(
        store,
        app.state.catalog.open(project_id).history,
        project_id,
        episode_id="ended-orchestrator",
        invocation_ceiling=2,
        report_error="Report unavailable.",
    )
    # Inspect the committed launch inputs before any provider is started.
    if not launch:
        monkeypatch.setattr(
            tasks, "launch_admitted", lambda operation_id: store.agent_task(operation_id)
        )
    return (
        app,
        store,
        original,
        f"/api/projects/{project_id}/episodes/{original.episode_id}/messages",
    )


def test_ended_message_claims_notice_and_mail_and_replays_exact_request(
    manifest, tmp_path, monkeypatch
):
    app, store, original, url = _ended_app(manifest, tmp_path, monkeypatch)
    body = {
        "body": "Investigate the alternative explanation.",
        "invocation_ceiling": 1,
        "request_id": str(uuid.uuid4()),
    }
    with signed_in_client(app) as client:
        sent = client.post(url, json=body)
        assert sent.status_code == 201, sent.text
        message = sent.json()
        continued = store.episode(message["episode_id"])
        assert continued is not None
        assert continued.continues_episode_id == original.episode_id
        assert continued.invocation_ceiling == 1
        assert continued.invocations_used == 1
        assert message["recipient_task_id"] == continued.root_operation_id
        assert message["delivery_operation_id"] == continued.root_operation_id
        assert message["authorized_by"] == continued.authorized_by.model_dump()
        with store.connection() as connection:
            notices = connection.execute(
                "SELECT source_event, delivery_operation_id FROM auto_research_lifecycle_notices WHERE episode_id = ?",
                (continued.episode_id,),
            ).fetchall()
        assert [(row["source_event"], row["delivery_operation_id"]) for row in notices] == [
            ("reauthorized", continued.root_operation_id)
        ]
        assert client.post(url, json=body).json() == message
        # A rename between a lost response and its retry is still the same member.
        store.rename_space_user(continued.authorized_by.user_id, "Renamed researcher")
        assert client.post(url, json=body).json() == message
        conflict = client.post(url, json={**body, "body": "A different instruction."})
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "message_request_conflict"
        for different_ceiling in [continued.invocation_ceiling + 1, None]:
            conflict = client.post(url, json={**body, "invocation_ceiling": different_ceiling})
            assert conflict.status_code == 409
            assert conflict.json()["detail"]["code"] == "message_request_conflict"
        assert (
            store.episode(continued.episode_id).invocation_ceiling == continued.invocation_ceiling
        )
        assert len(store.auto_research_messages(continued.episode_id)) == 1
        with pytest.raises(ValueError) as admission_conflict:
            continue_auto_research(
                app.state.background_tasks,
                original,
                invocation_ceiling=continued.invocation_ceiling + 1,
                request_id=body["request_id"],
                authorized_by=continued.authorized_by,
                message_body=body["body"],
                message_id=message["message_id"],
            )
        assert admission_conflict.value.args == ("message_request_conflict",)
        with pytest.raises(ValueError) as storage_conflict:
            store.create_auto_research_continuation(
                continued.model_copy(
                    update={
                        "status": "queued",
                        "root_operation_id": None,
                        "invocations_used": 0,
                        "invocation_ceiling": continued.invocation_ceiling + 1,
                    }
                ),
                store.auto_research_state(continued.episode_id),
                store.agent_task(continued.root_operation_id),
                store.auto_research_lifecycle_delivery(continued.root_operation_id)[0],
                store.auto_research_message(message["message_id"]),
            )
        assert storage_conflict.value.args == ("message_request_conflict",)


def test_a_sender_racing_a_continuation_mails_it_instead(manifest, tmp_path, monkeypatch):
    app, store, original, url = _ended_app(manifest, tmp_path, monkeypatch)
    projected = routes.auto_research_message_refusal
    first = {}

    def another_sender_continues_first(store_, episode):
        refusal = projected(store_, episode)
        if not first:
            # Sender A's continuation commits after sender B passed the ended check.
            first["episode"], _ = continue_auto_research(
                app.state.background_tasks,
                episode,
                invocation_ceiling=2,
                request_id=str(uuid.uuid4()),
                authorized_by=episode.authorized_by,
                message_body="Instruction A",
                message_id=str(uuid.uuid4()),
            )
        return refusal

    monkeypatch.setattr(routes, "auto_research_message_refusal", another_sender_continues_first)
    with signed_in_client(app) as client:
        reply = client.post(
            url,
            json={
                "body": "Instruction B",
                "invocation_ceiling": 5,
                "request_id": str(uuid.uuid4()),
            },
        )
    assert reply.status_code == 201, reply.text
    continuation = first["episode"]
    assert reply.json()["episode_id"] == continuation.episode_id
    assert reply.json()["recipient_task_id"] == continuation.root_operation_id
    assert store.episode_continuation(original.episode_id).episode_id == continuation.episode_id
    assert store.episode_continuation(continuation.episode_id) is None
    assert store.episode(continuation.episode_id).invocation_ceiling == 2
    assert len(store.auto_research_messages(continuation.episode_id)) == 2


def test_first_turn_reads_mail_and_old_endpoint_follows_an_ended_continuation(
    manifest, tmp_path, monkeypatch
):
    app, store, original, url = _ended_app(manifest, tmp_path, monkeypatch, launch=True)
    # This test ends the first continuation by hand below. The app's own settlement
    # may also start that wrap-up once the root turn goes quiet, with a different
    # fence, so whichever ran first decided the outcome; keep the test's path only.
    monkeypatch.setattr(
        EpisodeReconciler, "reconcile_auto_research_wrapup", lambda *_args, **_kwargs: False
    )
    seen = []

    async def stream(_project_id, kind, request, execution):
        assert kind == "auto_research"
        assert request.wake_cause == "lifecycle"
        mail = [
            message
            for message in store.auto_research_messages(request.episode_id)
            if message.delivery_operation_id == execution.operation_id
        ]
        notices = store.auto_research_lifecycle_delivery(execution.operation_id)
        assert len(mail) == len(notices) == 1
        assert notices[0].source_event == "reauthorized"
        assert mail[0].recipient_task_id == request.actor_operation_id == execution.operation_id
        seen.append((request.episode_id, mail[0].message_id, notices[0].source_id))
        yield _sse(AgentEvent(event="session", session_id=request.session_id))
        yield _sse(AgentEvent(event="done"))

    app.state.background_tasks.stream = stream
    with signed_in_client(app) as client:
        payload = {
            "body": "Follow the original evidence.",
            "invocation_ceiling": 3,
            "request_id": str(uuid.uuid4()),
        }
        first = client.post(url, json=payload)
        assert first.status_code == 201, first.text
        first_message = first.json()
        first_episode = store.episode(first_message["episode_id"])
        assert first_episode is not None
        wait_for_task(store, first_episode.root_operation_id, expect="succeeded")
        # End the first continuation through the existing report workflow.
        signal = auto_research_exhaustion_signal(store, first_episode.episode_id)
        begin_episode_report_wrapup(store, auto_research_wrapup_spec(store, signal))
        attempt = store.allocate_episode_report_attempt(first_episode.episode_id)
        store.finish_episode_report_error(attempt.attempt_id, "Fixture report unavailable.")
        assert store.episode(first_episode.episode_id).status == "needs_action"

        second = client.post(
            url,
            json={
                "body": "Now check the alternative.",
                "invocation_ceiling": 3,
                "request_id": str(uuid.uuid4()),
            },
        )
        assert second.status_code == 201, second.text
        second_message = second.json()
        second_episode = store.episode(second_message["episode_id"])
        assert second_episode is not None
        assert second_episode.continues_episode_id == first_episode.episode_id
        wait_for_task(store, second_episode.root_operation_id, expect="succeeded")
        assert seen == [
            (first_episode.episode_id, first_message["message_id"], original.episode_id),
            (second_episode.episode_id, second_message["message_id"], first_episode.episode_id),
        ]
        # A lost response still replays its original records after the chain advances.
        assert client.post(url, json=payload).json() == first_message
        changed_ceiling = client.post(url, json={**payload, "invocation_ceiling": 4})
        assert changed_ceiling.status_code == 409
        assert changed_ceiling.json()["detail"]["code"] == "message_request_conflict"
        assert len(seen) == 2


@pytest.mark.parametrize("ceiling", ["omitted", None])
def test_ordinary_mail_consent_cannot_start_a_continuation(
    manifest, tmp_path, monkeypatch, ceiling
):
    app, store, original, url = _ended_app(manifest, tmp_path, monkeypatch)
    body = {"body": "Ordinary mail.", "request_id": str(uuid.uuid4())}
    if ceiling != "omitted":
        body["invocation_ceiling"] = ceiling
    with signed_in_client(app) as client:
        response = client.post(url, json=body)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "auto_research_continuation_required"
    assert store.episode_continuation(original.episode_id) is None
    assert store.auto_research_messages(original.episode_id) == []
