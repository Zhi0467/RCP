from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from rcp.background import BackgroundAgentTasks
from rcp.core.authority import AgentDispatchAuthority, AgentDispatchScope
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.runs.auto_research import AutoResearchRunRequest
from rcp.runs.episodes.wrapup import EpisodeWrapupSpec, begin_episode_report_wrapup
from rcp.runs.watcher_admission import start_watcher_notification
from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.storage import (
    AgentTaskRecord,
    AppStore,
    AutoResearchStateRecord,
    EpisodeRecord,
    ProjectRecord,
    WatcherContinuation,
    WatcherRecord,
)

from .helpers import fabricated_authorizer, wait_for_task
from .test_background import _done_stream


def _store(tmp_path: Path) -> AppStore:
    store = AppStore(tmp_path / "rcp.sqlite3")
    store.upsert_project(
        ProjectRecord(
            project_id="project",
            locator=str(tmp_path / "research.yaml"),
            name="Project",
            state_location=str(tmp_path / ".research"),
            state_remote=False,
            added_at=store.now(),
        )
    )
    return store


def _orchestrator_authority(episode_id: str) -> AgentDispatchAuthority:
    return AgentDispatchAuthority(
        profile="orchestrator",
        task_contract="orchestrate",
        scope=AgentDispatchScope(
            run_truth_scope=["repo-a"],
            episode_id=episode_id,
            patch_kind="work",
        ),
    )


def _worker_authority(episode_id: str) -> AgentDispatchAuthority:
    return AgentDispatchAuthority(
        profile="ordinary",
        task_contract="work_auto",
        scope=AgentDispatchScope(
            run_truth_scope=["repo-a"],
            episode_id=episode_id,
            patch_kind="work",
        ),
    )


def _create_auto_episode(
    store: AppStore,
    *,
    episode_id: str | None = None,
    stage_root: str | None = None,
) -> tuple[EpisodeRecord, AgentTaskRecord]:
    episode_id = episode_id or str(uuid.uuid4())
    operation_id = f"root-{episode_id}"
    target = GraphTargetRef(kind="branch", branch_id=episode_id)
    base = GraphHeadRef(revision=0)
    authorizer = fabricated_authorizer("Branch owner")
    now = store.now()
    episode = EpisodeRecord(
        episode_id=episode_id,
        project_id="project",
        mode="auto_research",
        graph_target=target,
        graph_base_head=base,
        status="queued",
        invocation_ceiling=4,
        authorized_by=authorizer,
        created_at=now,
        updated_at=now,
    )
    task = AgentTaskRecord(
        operation_id=operation_id,
        project_id="project",
        episode_id=episode_id,
        graph_target=target,
        kind="auto_research",
        status="queued",
        request=AutoResearchRunRequest(
            episode_id=episode_id,
            role="orchestrator",
            actor_operation_id=operation_id,
            provider="codex",
            model="",
            reasoning="medium",
            run_on="local",
            run_truth_scope=["repo-a"],
        ).model_dump(mode="json"),
        created_at=now,
        updated_at=now,
        status_message="Queued",
        native_session_id="branch-session" if stage_root is not None else None,
        stage_root=stage_root,
        authorized_by=authorizer,
        dispatch_authority=_orchestrator_authority(episode_id),
    )
    return store.create_auto_research_episode_with_root_task(
        episode,
        AutoResearchStateRecord(
            episode_id=episode_id,
            created_at=now,
            updated_at=now,
        ),
        task,
    )


def _watcher(
    store: AppStore,
    episode: EpisodeRecord,
    root: AgentTaskRecord,
    *,
    watcher_id: str,
    status: str = "active",
) -> WatcherRecord:
    return WatcherRecord(
        watcher_id=watcher_id,
        project_id=episode.project_id,
        origin_operation_id=root.operation_id,
        origin_task_kind="auto_research",
        chat_id="shared-actor",
        episode_id=episode.episode_id,
        graph_target=episode.graph_target,
        execution_host="",
        check_command="true",
        log_path=f"/tmp/{watcher_id}.log",
        cwd="/tmp",
        continuation=WatcherContinuation(
            provider="codex",
            run_on="local",
            run_truth_scope=["repo-a"],
            patch_kind="work",
        ),
        status=status,
        created_at=store.now(),
        completed_at=store.now() if status == "completed" else None,
    )


def _merge_task(
    store: AppStore,
    episode: EpisodeRecord,
    operation_id: str,
    *,
    graph_target: GraphTargetRef | None = None,
    authority: AgentDispatchAuthority | None = None,
) -> AgentTaskRecord:
    now = store.now()
    return AgentTaskRecord(
        operation_id=operation_id,
        project_id=episode.project_id,
        episode_id=episode.episode_id,
        graph_target=graph_target or episode.graph_target,
        kind="branch_merge",
        status="queued",
        request={"branch_id": episode.episode_id},
        created_at=now,
        updated_at=now,
        status_message="Queued for human-authorized graph merge",
        authorized_by=episode.authorized_by,
        dispatch_authority=authority or _orchestrator_authority(episode.episode_id),
    )


def test_branch_target_round_trips_through_episode_task_watcher_and_report(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "branch-stage"
    stage.mkdir()
    episode, root = _create_auto_episode(store, stage_root=str(stage))
    watcher = _watcher(store, episode, root, watcher_id="branch-watcher")
    store.create_watchers([watcher])

    stored_episode = store.episode(episode.episode_id)
    stored_root = store.agent_task(root.operation_id)
    stored_watcher = store.watcher(watcher.watcher_id)
    assert stored_episode is not None
    assert stored_root is not None
    assert stored_watcher is not None
    assert stored_episode.graph_target == stored_root.graph_target == stored_watcher.graph_target
    assert stored_episode.graph_base_head == GraphHeadRef(revision=0)

    with pytest.raises(ValueError, match="watcher identity conflicts"):
        store._validate_idempotent_watcher(
            stored_watcher,
            watcher.model_copy(update={"graph_target": GraphTargetRef()}),
        )

    store.complete_agent_task(root.operation_id, applied_revision=None, result={})
    admission = begin_episode_report_wrapup(
        store,
        EpisodeWrapupSpec(
            episode_id=episode.episode_id,
            ending="completed",
            partial=False,
            continuation_operation_id=root.operation_id,
            receipt={"result": "bounded"},
        ),
    )
    assert admission.task is not None
    assert admission.task.graph_target == episode.graph_target
    assert admission.episode.graph_base_head == episode.graph_base_head


def test_cross_target_auto_research_continuation_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    episode, root = _create_auto_episode(store)
    worker_id = str(uuid.uuid4())
    other_target = GraphTargetRef(kind="branch", branch_id=str(uuid.uuid4()))
    now = store.now()
    continuation = AgentTaskRecord(
        operation_id=worker_id,
        project_id=episode.project_id,
        episode_id=episode.episode_id,
        graph_target=other_target,
        kind="auto_research",
        status="queued",
        request=AutoResearchRunRequest(
            episode_id=episode.episode_id,
            role="worker",
            actor_operation_id=worker_id,
            control_node_id="blk/worker-seat",
            run_truth_scope=["repo-a"],
        ).model_dump(mode="json"),
        created_at=now,
        updated_at=now,
        status_message="Queued",
        parent_operation_id=root.operation_id,
        authorized_by=episode.authorized_by,
        dispatch_authority=_worker_authority(episode.episode_id),
    )

    with pytest.raises(ValueError, match="cannot change its graph target"):
        store.create_auto_research_agent_task(continuation, role="worker")
    assert store.agent_task(worker_id) is None


def test_main_chat_cannot_reuse_a_branch_bound_conversation_or_session(tmp_path: Path) -> None:
    store = _store(tmp_path)
    episode, _root = _create_auto_episode(store)
    chat_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    stage = tmp_path / "branch-chat"
    stage.mkdir()
    now = store.now()
    branch_task = AgentTaskRecord(
        operation_id="branch-chat",
        project_id=episode.project_id,
        episode_id=episode.episode_id,
        graph_target=episode.graph_target,
        kind="node_chat",
        status="succeeded",
        request={
            "chat_id": chat_id,
            "session_id": None,
            "patch_kind": "work",
            "mode": "work",
        },
        created_at=now,
        updated_at=now,
        status_message="Stored branch conversation.",
        native_session_id=session_id,
        stage_root=str(stage),
    )
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        store._insert_agent_task(connection, branch_task, continuation_cause="fresh")

    def main_task(operation_id: str, *, requested_chat_id: str, requested_session: str | None):
        return AgentTaskRecord(
            operation_id=operation_id,
            project_id=episode.project_id,
            kind="node_chat",
            status="queued",
            request={
                "chat_id": requested_chat_id,
                "session_id": requested_session,
                "patch_kind": "work",
                "mode": "work",
            },
            created_at=now,
            updated_at=now,
            status_message="Queued main conversation.",
        )

    with pytest.raises(ValueError, match="another graph target"):
        store.create_agent_task(
            main_task("main-same-chat", requested_chat_id=chat_id, requested_session=None)
        )
    with pytest.raises(ValueError, match="another conversation or graph target"):
        store.create_agent_task(
            main_task(
                "main-same-session",
                requested_chat_id=str(uuid.uuid4()),
                requested_session=session_id,
            )
        )

    assert store.agent_task("main-same-chat") is None
    assert store.agent_task("main-same-session") is None


def test_completed_watcher_delivery_groups_do_not_cross_graph_targets(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first, first_root = _create_auto_episode(store)
    first_watcher = _watcher(
        store,
        first,
        first_root,
        watcher_id="first-branch-watcher",
        status="completed",
    )
    store.create_watchers([first_watcher])
    store.complete_agent_task(first_root.operation_id, applied_revision=None, result={})
    store.mark_episode_stop_skipped(first.episode_id)

    second, second_root = _create_auto_episode(store)
    second_watcher = _watcher(
        store,
        second,
        second_root,
        watcher_id="second-branch-watcher",
        status="completed",
    )
    store.create_watchers([second_watcher])

    groups = store.completed_watcher_groups()
    assert {tuple(item.watcher_id for item in group) for group in groups} == {
        (first_watcher.watcher_id,),
        (second_watcher.watcher_id,),
    }
    assert {group[0].graph_target.key for group in groups} == {
        first.graph_target.key,
        second.graph_target.key,
    }


def test_branch_merge_task_requires_ended_quiescent_branch_and_exact_authority(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    episode, root = _create_auto_episode(store)

    with pytest.raises(ValueError, match="not paused with all child work settled"):
        store.create_branch_merge_task(_merge_task(store, episode, "merge-active"))

    store.complete_agent_task(root.operation_id, applied_revision=None, result={})
    with pytest.raises(ValueError, match="not paused with all child work settled"):
        store.create_branch_merge_task(_merge_task(store, episode, "merge-not-ended"))

    store.mark_episode_stop_skipped(episode.episode_id)

    with pytest.raises(ValueError, match="visible attributed branch root"):
        store.create_branch_merge_task(
            _merge_task(store, episode, "merge-main", graph_target=GraphTargetRef())
        )

    other_target = GraphTargetRef(kind="branch", branch_id=str(uuid.uuid4()))
    with pytest.raises(ValueError, match="exact Auto-research episode"):
        store.create_branch_merge_task(
            _merge_task(store, episode, "merge-cross-target", graph_target=other_target)
        )

    wrong_authority = AgentDispatchAuthority(
        profile="ordinary",
        task_contract="work_auto",
        scope=AgentDispatchScope(
            run_truth_scope=["repo-a"],
            episode_id=episode.episode_id,
            patch_kind="work",
        ),
    )
    with pytest.raises(ValueError, match="exact graph-only orchestrator authority"):
        store.create_branch_merge_task(
            _merge_task(store, episode, "merge-wrong-authority", authority=wrong_authority)
        )

    accepted = store.create_branch_merge_task(_merge_task(store, episode, "merge-accepted"))
    assert accepted.graph_target == episode.graph_target
    assert accepted.dispatch_authority == _orchestrator_authority(episode.episode_id)
    merge_authority = store.agent_task_authority(episode.project_id, accepted.operation_id)
    assert merge_authority.apply_target == GraphTargetRef()

    replay = _merge_task(store, episode, accepted.operation_id).model_copy(
        update={
            "created_at": accepted.created_at,
            "updated_at": accepted.updated_at,
        }
    )
    replayed = store.create_branch_merge_task(replay)
    assert replayed.operation_id == accepted.operation_id

    with pytest.raises(ValueError, match="another merge is already active"):
        store.create_branch_merge_task(_merge_task(store, episode, "merge-duplicate"))


def _ordinary_branch_chat(store, episode, *, status="succeeded", parent=None, mode="work"):
    request = RunRequest(
        provider="codex",
        run_on="laptop",
        run_truth_scope=["repo-a"],
        chat_id=parent.request["chat_id"] if parent else str(uuid.uuid4()),
        chat_scope="project",
        message="Review the episode branch.",
        mode=mode,
    )
    now = store.now()
    return AgentTaskRecord(
        operation_id=str(uuid.uuid4()),
        project_id=episode.project_id,
        graph_target=episode.graph_target,
        kind="project_chat",
        status=status,
        request=request.model_dump(mode="json"),
        created_at=now,
        updated_at=now,
        status_message="Ordinary branch review.",
        authorized_by=episode.authorized_by,
        dispatch_authority=resolve_dispatch_authority("project_chat", request),
        parent_operation_id=parent.operation_id if parent else None,
        attempt=parent.attempt + 1 if parent else 1,
    )


def test_ordinary_branch_recovery_settles_and_merge_fences_new_chat(tmp_path):
    store = _store(tmp_path)
    episode, root = _create_auto_episode(store)
    previous = store.create_agent_task(_ordinary_branch_chat(store, episode, status="paused"))
    assert previous.episode_id is None
    store.complete_agent_task(root.operation_id, applied_revision=None, result={})
    store.mark_episode_stop_skipped(episode.episode_id)
    with pytest.raises(ValueError, match="active writer"):
        store.create_branch_merge_task(_merge_task(store, episode, "merge-before-recovery"))
    recovered = store.create_agent_task(
        _ordinary_branch_chat(store, episode, parent=previous), continuation_cause="retry"
    )
    assert recovered.episode_id is None
    assert store.unsettled_graph_target_tasks(episode.project_id, episode.graph_target) == []
    merge = store.create_branch_merge_task(_merge_task(store, episode, "merge-after-recovery"))
    with pytest.raises(ValueError, match="being merged"):
        store.create_agent_task(_ordinary_branch_chat(store, episode, status="queued"))
    store.complete_agent_task(merge.operation_id, applied_revision=None, result={})
    accepted = store.create_agent_task(_ordinary_branch_chat(store, episode, status="queued"))
    assert accepted.episode_id is None
    assert accepted.graph_target == episode.graph_target


def test_active_branch_merge_admits_discuss_and_refuses_work(tmp_path):
    store = _store(tmp_path)
    episode, root = _create_auto_episode(store)
    store.complete_agent_task(root.operation_id, applied_revision=None, result={})
    store.mark_episode_stop_skipped(episode.episode_id)
    merge = store.create_branch_merge_task(_merge_task(store, episode, "held-merge"))

    discuss = store.create_agent_task(
        _ordinary_branch_chat(store, episode, status="queued", mode="discuss")
    )
    assert discuss.episode_id is None
    assert discuss.graph_target == episode.graph_target
    assert discuss.request["mode"] == "discuss"
    assert discuss.dispatch_authority.task_contract == "discuss"
    assert discuss.dispatch_authority.scope.patch_kind is None
    assert store.agent_task(merge.operation_id).status == "queued"
    with pytest.raises(ValueError, match="being merged"):
        store.create_agent_task(_ordinary_branch_chat(store, episode, status="queued"))


def test_task_list_filters_graph_target_before_its_page_limit(tmp_path):
    store = _store(tmp_path)
    episode, _root = _create_auto_episode(store)
    branch_task = store.create_agent_task(_ordinary_branch_chat(store, episode))
    main_task = store.create_agent_task(
        _ordinary_branch_chat(store, episode).model_copy(update={"graph_target": GraphTargetRef()})
    )
    assert store.agent_tasks(episode.project_id, limit=1)[0].operation_id == main_task.operation_id
    assert (
        store.agent_tasks(episode.project_id, limit=1, graph_target=episode.graph_target)[
            0
        ].operation_id
        == branch_task.operation_id
    )


def test_ordinary_branch_watcher_wakes_after_episode_ends_without_episode_budget(tmp_path):
    store = _store(tmp_path)
    episode, root = _create_auto_episode(store)
    origin = store.create_agent_task(_ordinary_branch_chat(store, episode))
    store.complete_agent_task(root.operation_id, applied_revision=None, result={})
    store.mark_episode_stop_skipped(episode.episode_id)
    watcher = WatcherRecord(
        watcher_id="ordinary-branch-watcher",
        project_id=episode.project_id,
        origin_operation_id=origin.operation_id,
        origin_task_kind=origin.kind,
        chat_id=origin.request["chat_id"],
        graph_target=episode.graph_target,
        execution_host="",
        check_command="true",
        log_path="/tmp/branch-review.log",
        cwd="/tmp",
        continuation=WatcherContinuation(
            provider="codex", run_on="laptop", run_truth_scope=["repo-a"]
        ),
        status="completed",
        created_at=store.now(),
        completed_at=store.now(),
    )
    store.create_watchers([watcher])
    tasks = BackgroundAgentTasks(store, _done_stream)
    request = RunRequest.model_validate(
        {**origin.request, "trigger": "watcher", "watcher_ids": [watcher.watcher_id]}
    )
    wake = start_watcher_notification(
        tasks,
        episode.project_id,
        origin.kind,
        request,
        [watcher.watcher_id],
        authorized_by=episode.authorized_by,
    )
    assert wake is not None
    finished = wait_for_task(store, wake.operation_id)
    assert finished.status == "succeeded", finished.error
    assert finished.graph_target == episode.graph_target
    assert finished.episode_id is None
    assert store.watcher(watcher.watcher_id).notified


@pytest.mark.parametrize("winner", ["chat", "merge"])
def test_ordinary_branch_work_and_merge_admission_are_atomic(tmp_path, monkeypatch, winner):
    store = _store(tmp_path)
    episode, root = _create_auto_episode(store)
    store.complete_agent_task(root.operation_id, applied_revision=None, result={})
    store.mark_episode_stop_skipped(episode.episode_id)
    chat = _ordinary_branch_chat(store, episode, status="queued")
    merge = _merge_task(store, episode, "merge-race")
    entered = threading.Event()
    release = threading.Event()
    original_insert = store._insert_agent_task
    first_id = chat.operation_id if winner == "chat" else merge.operation_id

    def hold_winner(connection, record, **kwargs):
        if record.operation_id == first_id:
            entered.set()
            assert release.wait(10)
        return original_insert(connection, record, **kwargs)

    monkeypatch.setattr(store, "_insert_agent_task", hold_winner)
    admissions = {
        "chat": lambda: store.create_agent_task(chat),
        "merge": lambda: store.create_branch_merge_task(merge),
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(admissions[winner])
        try:
            assert entered.wait(10)
            second = pool.submit(admissions["merge" if winner == "chat" else "chat"])
        finally:
            release.set()
        assert first.result().operation_id == first_id
        with pytest.raises(ValueError, match="active writer|being merged"):
            second.result()
