from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest

from rcp.agents import AgentEvent
from rcp.background import BackgroundAgentTasks
from rcp.machine_cards import BACKEND_MACHINE_ID, LoginKey
from rcp.runs.auto_research_admission import start_auto_research_child_work
from rcp.runs.auto_research_delivery import (
    deliver_auto_research_watcher_group,
    deliver_pending_auto_research_lifecycle,
    deliver_pending_auto_research_mail,
    record_auto_research_message,
)
from rcp.service import RunRequest
from rcp.storage import AutoResearchLifecycleNoticeRecord
from rcp.storage.models import MACHINE_LOGIN_ID, _required_timestamp

from .helpers import fake_launch_admitted
from .test_auto_research_child_work_watchers import _deliver, _waiting_child
from .test_auto_research_delivery import (
    _arm_completed_graph_condition,
    _sse,
    _start_auto_research,
    _store,
)


def _signed_out(store):
    store.mark_provider_login_failed(
        LoginKey("codex", BACKEND_MACHINE_ID, MACHINE_LOGIN_ID),
        generation=0,
        detail="refresh_token_reused",
        source="turn",
    )


def _counts(store):
    with store.connection() as connection:
        return (
            connection.execute("SELECT COUNT(*) FROM graph_runs").fetchone()[0],
            connection.execute(
                "SELECT COALESCE(SUM(invocations_used), 0) FROM episodes"
            ).fetchone()[0],
        )


def _idle_root(tmp_path):
    store = _store(tmp_path)
    stage = tmp_path / "stage"
    stage.mkdir()

    async def stream(_project_id, _kind, request, execution):
        execution.checkpoint_stage(BACKEND_MACHINE_ID, str(stage))
        yield _sse(AgentEvent(event="session", session_id=request.session_id or "root-session"))
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    episode, root = _start_auto_research(tasks)
    return tasks, episode, root


def test_signed_out_root_start_creates_no_task_or_budget(tmp_path):
    store = _store(tmp_path)

    async def stream(*_args):
        raise AssertionError("A signed-out account must not launch")
        yield ""

    tasks = BackgroundAgentTasks(store, stream)
    _signed_out(store)
    before = _counts(store)
    with pytest.raises(ValueError, match="sign"):
        _start_auto_research(tasks)
    assert _counts(store) == before
    assert store.episode("auto_research") is None


def test_signed_out_child_work_creates_no_task_or_budget(tmp_path):
    tasks, episode, root = _idle_root(tmp_path)
    _signed_out(tasks.store)
    before = _counts(tasks.store)
    instruction = "Inspect the pending result."
    with pytest.raises(ValueError, match="sign"):
        start_auto_research_child_work(
            tasks,
            episode.episode_id,
            RunRequest(
                provider="codex",
                run_on=BACKEND_MACHINE_ID,
                run_truth_scope=["repo-a"],
                chat_scope="node",
                node_id="blk/result",
                chat_id="00000000-0000-4000-8000-000000000451",
                message=instruction,
                mode="work",
                trigger="orchestrator",
                patch_kind="work",
            ),
            admitted_by_operation_id=root.operation_id,
            worker_id="00000000-0000-4000-8000-000000000451",
            instruction=instruction,
            instruction_sha256=hashlib.sha256(instruction.encode()).hexdigest(),
        )
    assert _counts(tasks.store) == before
    assert tasks.store.auto_research_child_works(episode.episode_id) == []


@pytest.mark.parametrize("wake", ["mail", "lifecycle", "watcher"])
def test_signed_out_root_wakes_preserve_pending_inputs_and_budget(tmp_path, monkeypatch, wake):
    tasks, episode, root = _idle_root(tmp_path)
    store = tasks.store
    message = record_auto_research_message(
        store,
        episode_id=episode.episode_id,
        sender_role="human",
        sender_task_id=None,
        authorized_by=episode.authorized_by,
        recipient_task_id=root.operation_id,
        body="Inspect the pending result.",
    )
    watcher = _arm_completed_graph_condition(store, episode, root)
    notices = []
    if wake != "mail":
        notices.append(
            store.record_auto_research_lifecycle_notice(
                AutoResearchLifecycleNoticeRecord(
                    notice_id="result-ready",
                    episode_id=episode.episode_id,
                    source_kind="worker",
                    source_id="worker-result",
                    source_event="succeeded",
                    payload={},
                    created_at=store.now(),
                )
            )
        )
    after_grace = _required_timestamp(store.now()) + timedelta(minutes=1)
    monkeypatch.setattr(store, "now", lambda: after_grace.isoformat())
    _signed_out(store)
    before = _counts(store)
    for _ in range(2):
        if wake == "mail":
            result = deliver_pending_auto_research_mail(
                tasks, episode_id=episode.episode_id, recipient_task_id=root.operation_id
            )
        elif wake == "lifecycle":
            result = deliver_pending_auto_research_lifecycle(tasks, episode_id=episode.episode_id)
        else:
            result = deliver_auto_research_watcher_group(tasks, [watcher])
        assert result is None
        assert _counts(store) == before
        assert store.pending_auto_research_messages(episode.episode_id, root.operation_id) == [
            message
        ]
        assert store.pending_auto_research_lifecycle_notices(episode.episode_id) == notices
        assert store.watcher(watcher.watcher_id).notified is False


@pytest.mark.parametrize("wake", ["mail", "watcher"])
def test_signed_out_child_wakes_preserve_pending_inputs_and_budget(tmp_path, wake):
    tasks, episode, child, watchers, _seen = _waiting_child(tmp_path)
    store = tasks.store
    message = record_auto_research_message(
        store,
        episode_id=episode.episode_id,
        sender_role="orchestrator",
        sender_task_id=episode.root_operation_id,
        authorized_by=None,
        recipient_task_id=watchers[0].worker_id,
        body="Inspect the pending result.",
    )
    _signed_out(store)
    before = _counts(store)
    for _ in range(2):
        result = (
            _deliver(tasks, episode, watchers)
            if wake == "watcher"
            else deliver_pending_auto_research_mail(
                tasks, episode_id=episode.episode_id, recipient_task_id=watchers[0].worker_id
            )
        )
        assert result is None
        assert _counts(store) == before
        assert store.pending_auto_research_messages(episode.episode_id, watchers[0].worker_id) == [
            message
        ]
        assert all(store.watcher(watcher.watcher_id).notified is False for watcher in watchers)


def test_signed_out_child_experiment_creates_no_task_or_budget(manifest, tmp_path):
    from .test_auto_research_experiments import (
        EXPERIMENT_ID,
        _setup,
        _spend_child_experiment_allowance,
    )

    _service, store, tasks, _coordinator, parent_id, root_id = _setup(manifest, tmp_path)
    _signed_out(store)
    before = _counts(store)
    with pytest.raises(ValueError, match="sign"):
        _spend_child_experiment_allowance(
            store,
            tasks,
            parent_episode_id=parent_id,
            parent_operation_id=root_id,
            child_episode_id="00000000-0000-4000-8000-000000000499",
            node_id=EXPERIMENT_ID,
        )
    assert _counts(store) == before


def test_signed_out_experiment_start_creates_no_task_or_budget(manifest, tmp_path):
    from .helpers import fabricated_authorizer
    from .test_auto_research_experiments import EXPERIMENT_ID, PROJECT_ID, _setup

    _service, store, tasks, _coordinator, _parent_id, _root_id = _setup(manifest, tmp_path)
    request = RunRequest(
        provider="codex",
        run_on=BACKEND_MACHINE_ID,
        run_truth_scope=["repo-a"],
        chat_scope="node",
        node_id=EXPERIMENT_ID,
        message="Inspect the result.",
        mode="work",
        trigger="experiment_run",
        patch_kind="experiment_loop",
        control_node_id=EXPERIMENT_ID,
        control_revision=1,
        control_episode_id="00000000-0000-4000-8000-000000000498",
        chat_id="00000000-0000-4000-8000-000000000498",
        control_invocation=1,
        control_invocation_ceiling=2,
        control_decision_bundle=[],
        control_completion_criteria=["Inspect the result."],
    )
    _signed_out(store)
    before = _counts(store)
    with pytest.raises(ValueError, match="sign"):
        tasks.start(PROJECT_ID, "node_chat", request, authorized_by=fabricated_authorizer())
    assert _counts(store) == before


@pytest.mark.parametrize("kind", ["node_chat", "project_chat"])
@pytest.mark.parametrize("mode", ["work", "discuss"])
def test_signed_out_ordinary_chat_creates_no_task(tmp_path, kind, mode):
    from rcp.runs.provider_login import ProviderSignedOut

    from .helpers import fabricated_authorizer

    store = _store(tmp_path)

    async def stream(*_args):
        raise AssertionError("A signed-out account must not launch")
        yield ""

    tasks = BackgroundAgentTasks(store, stream)
    _signed_out(store)
    request = RunRequest(
        provider="codex",
        run_on=BACKEND_MACHINE_ID,
        chat_scope="node" if kind == "node_chat" else "project",
        node_id="blk/result" if kind == "node_chat" else None,
        message="Inspect the result.",
        mode=mode,
    )
    assert _counts(store)[0] == 0
    with pytest.raises(ProviderSignedOut):
        tasks.start("project", kind, request, authorized_by=fabricated_authorizer())
    assert _counts(store)[0] == 0


@pytest.mark.parametrize("experiment", [False, True])
def test_committed_task_waits_for_verify_without_poll_writes(
    manifest, tmp_path, monkeypatch, experiment
):
    from .helpers import fabricated_authorizer, wait_for_task, wait_until
    from .test_background import _experiment_request
    from .test_provider_login_resume import _verify_client

    store = _store(tmp_path)
    store.upsert_project(
        store.project("project").model_copy(update={"locator": str(manifest.path)})
    )
    calls = []

    async def stream(_project, _kind, _request, execution):
        calls.append(execution.operation_id)
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    request = (
        _experiment_request()
        if experiment
        else RunRequest(
            provider="codex",
            run_on=BACKEND_MACHINE_ID,
            chat_scope="node",
            node_id="blk/result",
            chat_id="ordinary-chat",
            run_truth_scope=["repo-a"],
            message="Inspect the result.",
            mode="work",
        )
    )
    with monkeypatch.context() as admission:
        fake_launch_admitted(admission, tasks)
        queued = tasks.start("project", "node_chat", request, authorized_by=fabricated_authorizer())
    _signed_out(store)
    before = store.agent_task_receipts(queued.operation_id)
    for _ in range(2):
        assert tasks.launch_admitted(queued.operation_id) == queued
        assert store.agent_task(queued.operation_id) == queued
        assert store.agent_task_receipts(queued.operation_id) == before
        assert calls == []
        assert not tasks._workers
    client = _verify_client(store, tasks, monkeypatch)
    response = client.post(
        "/api/providers/codex/logins/verify", json={"machine_id": BACKEND_MACHINE_ID}
    )
    assert response.status_code == 200, response.text
    assert response.json()["resumed"]["checked"] >= 1
    wait_for_task(store, queued.operation_id, expect="succeeded")
    wait_until(lambda: queued.operation_id not in tasks._workers)
    again = client.post(
        "/api/providers/codex/logins/verify", json={"machine_id": BACKEND_MACHINE_ID}
    )
    assert again.status_code == 200, again.text
    assert again.json()["resumed"]["checked"] == int(experiment)
    assert calls == [queued.operation_id]


def test_verify_dispatches_committed_lifecycle_wake_once(manifest, tmp_path, monkeypatch):
    from .helpers import wait_for_task, wait_until
    from .test_provider_login_resume import _verify_client

    tasks, episode, root = _idle_root(tmp_path)
    store = tasks.store
    store.upsert_project(
        store.project("project").model_copy(update={"locator": str(manifest.path)})
    )
    wait_until(lambda: root.operation_id not in tasks._workers)
    store.record_auto_research_lifecycle_notice(
        AutoResearchLifecycleNoticeRecord(
            notice_id="queued-result",
            episode_id=episode.episode_id,
            source_kind="worker",
            source_id="worker-result",
            source_event="succeeded",
            payload={},
            created_at=store.now(),
        )
    )
    after_grace = _required_timestamp(store.now()) + timedelta(minutes=1)
    monkeypatch.setattr(store, "now", lambda: after_grace.isoformat())
    with monkeypatch.context() as admission:
        fake_launch_admitted(admission, tasks)
        operation_id = deliver_pending_auto_research_lifecycle(tasks, episode_id=episode.episode_id)
    assert operation_id is not None
    queued = store.agent_task(operation_id)
    assert queued.status == "queued"
    _signed_out(store)
    receipts = store.agent_task_receipts(operation_id)
    before = _counts(store)
    for _ in range(2):
        assert tasks.launch_admitted(operation_id) == queued
        assert store.agent_task_receipts(operation_id) == receipts
        assert not tasks._workers
    client = _verify_client(store, tasks, monkeypatch)
    response = client.post(
        "/api/providers/codex/logins/verify", json={"machine_id": BACKEND_MACHINE_ID}
    )
    assert response.status_code == 200, response.text
    assert response.json()["resumed"]["checked"] >= 1
    wait_for_task(store, operation_id, expect="succeeded")
    wait_until(lambda: operation_id not in tasks._workers)
    assert _counts(store) == before
    again = client.post(
        "/api/providers/codex/logins/verify", json={"machine_id": BACKEND_MACHINE_ID}
    )
    assert again.status_code == 200, again.text
    assert again.json()["resumed"]["checked"] >= 1
    assert _counts(store) == before


def test_missing_managed_credential_refuses_without_existing_login_state(tmp_path):
    from rcp.runs.provider_login import ProviderSignedOut

    from .helpers import fabricated_authorizer

    store = _store(tmp_path)

    async def stream(*_args):
        raise AssertionError("Missing credentials must refuse admission")
        yield ""

    tasks = BackgroundAgentTasks(store, stream)
    assert store.provider_login_states() == []
    before = _counts(store)
    with pytest.raises(ProviderSignedOut):
        tasks.start(
            "project",
            "project_chat",
            RunRequest(
                provider="claude",
                run_on=BACKEND_MACHINE_ID,
                chat_scope="project",
                message="Inspect results",
                mode="work",
            ),
            authorized_by=fabricated_authorizer(),
        )
    assert _counts(store) == before
    assert store.provider_login_states() == []


def test_run_on_names_its_card_and_defaults_to_the_base_machine(tmp_path):
    from rcp.runs.provider_login import run_on_machine

    store = _store(tmp_path)
    gpu = store.create_space_machine(name="gpu", host="gpu.example", os_account="")
    assert run_on_machine(store, "project", gpu.machine_id) == gpu.machine_id
    assert run_on_machine(store, "project", None) == BACKEND_MACHINE_ID
    for run_on in (gpu.machine_id, None):
        assert run_on_machine(store, store.space_id, run_on) == BACKEND_MACHINE_ID


def _limited_chat(tmp_path, monkeypatch):
    """A succeeded chat turn whose only sign-in then hit its usage limit."""
    from datetime import datetime

    from rcp.storage import WatcherContinuation, WatcherRecord

    from .helpers import fabricated_authorizer, wait_for_task
    from .lifecycles.agent_task import seeded_store, work_request

    store = seeded_store(tmp_path)
    clock = [datetime.fromisoformat(store.now())]
    monkeypatch.setattr(store, "now", lambda: clock[0].isoformat())
    stage = tmp_path / "chat-stage"
    stage.mkdir()

    async def stream(_project_id, _kind, _request, execution):
        execution.checkpoint_stage(BACKEND_MACHINE_ID, str(stage))
        yield _sse(AgentEvent(event="session", session_id="chat-session"))
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    authorizer = fabricated_authorizer("Researcher")
    request = work_request("limited-chat")
    first = tasks.start("project", "project_chat", request, authorized_by=authorizer)
    first = wait_for_task(store, first.operation_id, expect="succeeded")
    login = LoginKey("codex", BACKEND_MACHINE_ID, first.login_id or MACHINE_LOGIN_ID)
    assert store.set_provider_login_limit(
        login, provider_started_at=(clock[0] - timedelta(seconds=1)).isoformat(), limited=True
    )
    store.create_watchers(
        [
            WatcherRecord(
                watcher_id="limited-watch",
                project_id="project",
                origin_operation_id=first.operation_id,
                origin_task_kind="project_chat",
                chat_id=request.chat_id,
                check_command="true",
                cwd=str(stage),
                log_path=str(stage / "watch.log"),
                continuation=WatcherContinuation(
                    provider="codex",
                    model="",
                    reasoning="medium",
                    run_on=BACKEND_MACHINE_ID,
                    run_truth_scope=["repo"],
                    patch_kind="work",
                ),
                status="completed",
                created_at=store.now(),
                completed_at=store.now(),
            )
        ]
    )
    return store, tasks, clock, authorizer, request


def test_watcher_wake_waits_for_a_limited_login_to_lapse(tmp_path, monkeypatch):
    from rcp.limits import PROVIDER_LOGIN_LIMIT_HINT_SECONDS
    from rcp.runs.watcher_admission import start_watcher_notification

    from .helpers import wait_for_task

    store, tasks, clock, authorizer, request = _limited_chat(tmp_path, monkeypatch)
    wake_request = request.model_copy(
        update={"trigger": "watcher", "watcher_ids": ["limited-watch"]}
    )

    def wake():
        return start_watcher_notification(
            tasks,
            "project",
            "project_chat",
            wake_request,
            ["limited-watch"],
            authorized_by=authorizer,
        )

    before = _counts(store)
    assert wake() is None
    assert _counts(store) == before
    assert store.watcher("limited-watch").status == "completed"
    clock[0] += timedelta(seconds=PROVIDER_LOGIN_LIMIT_HINT_SECONDS)
    started = wake()
    assert started is not None
    wait_for_task(store, started.operation_id, expect="succeeded")


def test_human_turn_launches_on_a_limited_login(tmp_path, monkeypatch):
    from .helpers import wait_for_task

    store, tasks, _clock, authorizer, request = _limited_chat(tmp_path, monkeypatch)
    turn = tasks.start("project", "project_chat", request, authorized_by=authorizer)
    wait_for_task(store, turn.operation_id, expect="succeeded")
