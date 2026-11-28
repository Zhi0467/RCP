"""An idle Experiment episode wakes once for new mail or a Bulletin mention."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, cast

from rcp.background import BackgroundAgentTasks
from rcp.core.transition_models import GraphTargetRef
from rcp.machine_cards import BACKEND_MACHINE_ID
from rcp.runs.episode_mailbox import wake_bulletin_mentions
from rcp.runs.experiment_loop import experiment_loop_phase, wake_experiment_for_mail
from rcp.service import RunRequest
from rcp.storage import AppStore
from rcp.storage.models import AutoResearchMessageRecord

from .helpers import fake_launch_admitted
from .test_auto_research_children_storage import _project
from .test_experiment_episode_storage import _admit_root, _identity


async def _no_provider(*_args, **_kwargs):
    raise AssertionError("Mail wake tests never start a provider.")
    yield


def _idle_episode(tmp_path: Path, *, ceiling: int = 3, settled: bool = True):
    store = AppStore(tmp_path / "rcp.sqlite3")
    episode_id, root = _admit_root(store, ceiling=ceiling)
    stage = tmp_path / "stage"
    stage.mkdir()
    store.commit_experiment_episode_turn(
        episode_id=episode_id,
        project_id="project",
        control_node_id="exp-one",
        provider="codex",
        machine_id=BACKEND_MACHINE_ID,
        native_session_id="native-session",
        stage_machine_id=BACKEND_MACHINE_ID,
        stage_root=str(stage),
        chat_id="episode-chat",
        operation_id=root.operation_id,
        invocation=1,
        graph_result="no graph change",
        watcher_ids=[],
        context_baseline={},
    )
    if settled:
        store.complete_agent_task(root.operation_id, applied_revision=None, result={})
    return store, episode_id


def _used(store: AppStore, episode_id: str) -> int:
    episode = store.episode(episode_id)
    assert episode is not None
    return episode.invocations_used


def _tasks(monkeypatch, store: AppStore, launches: list[str]) -> BackgroundAgentTasks:
    tasks = BackgroundAgentTasks(store, _no_provider)
    monkeypatch.setattr(tasks, "admit_provider_task", lambda *_args, **_kwargs: None)
    fake_launch_admitted(monkeypatch, tasks, launches.append)
    return tasks


def _mail(store: AppStore, episode_id: str, body: str = "Try the smaller batch.") -> str:
    message_id = str(uuid.uuid4())
    store.record_episode_message(
        AutoResearchMessageRecord(
            message_id=message_id,
            episode_id=episode_id,
            sender_role="human",
            authorized_by=_identity(store),
            recipient_kind="episode",
            body=body,
            created_at=store.now(),
        )
    )
    return message_id


def test_idle_episode_wakes_once_and_the_wake_claims_the_mail(tmp_path, monkeypatch):
    store, episode_id = _idle_episode(tmp_path)
    launches: list[str] = []
    tasks = _tasks(monkeypatch, store, launches)
    message_id = _mail(store, episode_id)

    assert wake_experiment_for_mail(tasks, episode_id, cause="mail") == "now"
    [wake] = launches
    task = store.agent_task(wake)
    assert task is not None and task.episode_id == episode_id
    assert task.native_session_id == "native-session"
    assert experiment_loop_phase(RunRequest.model_validate(task.request), "message_wake") == (
        "mail_wake"
    )
    assert _used(store, episode_id) == 2
    assert [
        item.message_id for item in store.claim_episode_mail("episode", episode_id, [wake])
    ] == [message_id]
    assert wake_experiment_for_mail(tasks, episode_id, cause="mail") == "next_turn"
    assert len(launches) == 1


def test_busy_episode_reads_mail_at_its_next_turn(tmp_path, monkeypatch):
    store, episode_id = _idle_episode(tmp_path, settled=False)
    launches: list[str] = []
    tasks = _tasks(monkeypatch, store, launches)
    _mail(store, episode_id)

    assert wake_experiment_for_mail(tasks, episode_id, cause="mail") == "next_turn"
    assert launches == []
    assert _used(store, episode_id) == 1


def test_episode_at_its_ceiling_is_not_woken(tmp_path, monkeypatch):
    store, episode_id = _idle_episode(tmp_path, ceiling=1)
    launches: list[str] = []
    tasks = _tasks(monkeypatch, store, launches)
    _mail(store, episode_id)

    assert wake_experiment_for_mail(tasks, episode_id, cause="mail") == "next_turn"
    assert launches == []
    assert [item.delivered_at for item in store.pending_episode_mail("episode", episode_id)] == [
        None
    ]


def test_mail_from_a_mail_woken_turn_does_not_wake(tmp_path, monkeypatch):
    store, episode_id = _idle_episode(tmp_path)
    launches: list[str] = []
    tasks = _tasks(monkeypatch, store, launches)
    message_id = _mail(store, episode_id)
    with store.connection() as connection:
        connection.execute(
            "UPDATE auto_research_messages SET sender_task_id='loop-root' WHERE message_id=?",
            (message_id,),
        )
        connection.execute(
            "UPDATE graph_runs SET request_json=json_set(request_json,'$.chat_wake',"
            'json(\'{"kind":"episode_mail","item_ids":[],"chains":[]}\')) '
            "WHERE operation_id='loop-root'"
        )

    assert wake_experiment_for_mail(tasks, episode_id, cause="mail") == "next_turn"
    assert launches == []


def test_a_mention_wakes_the_idle_experiment_once(tmp_path, monkeypatch):
    store, episode_id = _idle_episode(tmp_path)
    launches: list[str] = []
    tasks = _tasks(monkeypatch, store, launches)
    user_id = _identity(store).user_id
    store.on_wake_input = lambda *_args: None
    _project(store)
    if not store.is_project_member("project", user_id):
        store.seat_project_member("project", user_id)
    post = store.mutate_bulletin_post(
        "project",
        GraphTargetRef().key,
        user_id=user_id,
        key="mention",
        text="<@agent:chat:episode-chat> the data moved",
    )["post_id"]

    assert wake_bulletin_mentions(tasks, "project", post) == {"chat:episode-chat": "now"}
    [wake] = launches
    woken = store.agent_task(wake)
    assert woken is not None and woken.episode_id == episode_id
    assert experiment_loop_phase(RunRequest.model_validate(woken.request), "message_wake") == (
        "mention_wake"
    )
    assert wake_bulletin_mentions(tasks, "project", post) == {"chat:episode-chat": "next_turn"}
    assert len(launches) == 1


def test_a_sender_can_queue_mail_without_waking(tmp_path, monkeypatch):
    from rcp.api.episode_routes import send_episode_message_as
    from rcp.api.episodes import EpisodeMessageBody

    store, episode_id = _idle_episode(tmp_path)
    launches: list[str] = []
    tasks = _tasks(monkeypatch, store, launches)
    episode = store.episode(episode_id)
    assert episode is not None

    def send(wake: bool):
        return send_episode_message_as(
            episode,
            EpisodeMessageBody(body="Check the queue.", wake=wake),
            authorized_by=_identity(store),
            catalog=cast(Any, None),
            background_tasks=tasks,
        )

    queued = send(wake=False)
    assert not queued.woke and launches == [] and _used(store, episode_id) == 1
    woken = send(wake=True)
    assert woken.woke and len(launches) == 1 and _used(store, episode_id) == 2
