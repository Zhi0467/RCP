from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier

import pytest

from rcp.storage import AgentTaskRecord, AppStore
from rcp.storage.models import AgentTaskAdmissionConflict

from .test_storage import _project


@pytest.fixture
def store(tmp_path):
    store = AppStore(tmp_path / "state.sqlite3")
    store.upsert_project(_project("project"))
    return store


def _task(store, operation_id, **updates):
    now = store.now()
    return AgentTaskRecord(
        operation_id=operation_id,
        project_id="project",
        kind="node_chat",
        status="queued",
        request={"provider": "codex", "session_id": "native"},
        native_session_id="native",
        stage_root="/scratch/native",
        created_at=now,
        updated_at=now,
        status_message="Queued",
    ).model_copy(update=updates)


def test_native_session_admission_is_atomic_across_distinct_launch_owners(store):
    ready = Barrier(2)

    def admit(operation_id):
        task = _task(store, operation_id)
        if operation_id == "edit":
            task = task.model_copy(
                update={
                    "kind": "artifact_edit",
                    "request": {
                        **task.request,
                        "artifact_edit": {"base_version_id": "base"},
                    },
                }
            )
        ready.wait()
        try:
            if task.kind == "artifact_edit":
                store.create_artifact_edit_task(task)
            else:
                store.create_agent_task(task)
        except AgentTaskAdmissionConflict:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(admit, ("chat", "edit")))
    assert sorted(results) == [False, True]
    winner = "chat" if results[0] else "edit"
    store.fail_agent_task(winner, "Finished")
    store.create_agent_task(_task(store, "next"))


@pytest.mark.parametrize("collision", ["session", "stage", "checkpoint"])
def test_launch_ownership_covers_session_and_stage_binding(store, collision):
    store.create_agent_task(_task(store, "owner"))
    binding = {
        "session": {"stage_root": "/scratch/other"},
        "stage": {"native_session_id": "other"},
        "checkpoint": {"native_session_id": None, "stage_root": None},
    }[collision]
    competing = _task(store, "competing", request={"provider": "codex"}, **binding)
    if collision == "checkpoint":
        store.create_agent_task(competing)
        with pytest.raises(AgentTaskAdmissionConflict):
            store.checkpoint_agent_task(
                "competing", native_session_id="native", stage_root="/scratch/native"
            )
        assert store.agent_task("competing").native_session_id is None
    else:
        with pytest.raises(AgentTaskAdmissionConflict):
            store.create_agent_task(competing)


def test_only_operational_success_reopens_master_after_revoking_edit(store):
    clock = datetime.fromisoformat(store.now())
    store.now = lambda: clock.isoformat()

    def pending():
        return store.episode_report_rebootstrap_pending(
            "project",
            "native",
            stage_host=None,
            stage_root="/scratch/native",
            owner="chat",
        )

    for kind in ("artifact_edit", "node_chat"):
        store.create_artifact_edit_task(
            _task(
                store,
                kind,
                kind=kind,
                request={
                    "provider": "codex",
                    "artifact_edit": {"base_version_id": "base", "launch_kind": "revoking"},
                },
            )
        )
        store.complete_agent_task(kind, result={}, applied_revision=None)
        assert pending()
        clock += timedelta(seconds=1)
    clock += timedelta(seconds=1)
    store.create_agent_task(_task(store, "operation"))
    store.complete_agent_task("operation", result={}, applied_revision=None)
    assert pending()
    from rcp.runs.session_master import record_session_master

    clock += timedelta(seconds=1)
    store.create_agent_task(_task(store, "reopened"))
    record_session_master(store, "reopened", "chat master")
    assert pending()
    store.complete_agent_task("reopened", result={}, applied_revision=None)
    assert not pending()


def test_orchestrator_edit_records_delivered_message_without_changing_stopped_episode(store):
    from .test_auto_research_children_storage import _auto_parent

    episode, root = _auto_parent(store)
    store.fail_agent_task(root.operation_id, "Ended")
    store.request_episode_stop(episode.episode_id)
    before = store.episode(episode.episode_id)
    edit = _task(
        store,
        "orchestrator-edit",
        kind="artifact_edit",
        graph_target=episode.graph_target,
        authorized_by=episode.authorized_by,
        request={
            "provider": "codex",
            "message": "Update the chart legend.",
            "artifact_edit": {
                "reply_episode_id": episode.episode_id,
                "episode_id": episode.episode_id,
                "operation_id": "edit-root",
            },
        },
    )
    store.create_artifact_edit_task(edit)
    assert store.episode(episode.episode_id) == before
    _, messages = store.auto_research_message_history(episode.episode_id, limit=10)
    assert len(messages) == 1
    assert messages[0].recipient_task_id == root.operation_id
    assert messages[0].delivery_operation_id == edit.operation_id
    assert messages[0].delivered_at is not None
    assert [task.operation_id for task in store.episode_tasks(episode.episode_id)] == [
        root.operation_id
    ]
    store.fail_agent_task(edit.operation_id, "Retry needed")
    retry = edit.model_copy(update={"operation_id": "retry-edit"})
    store.create_artifact_edit_task(retry, continuation_cause="retry")
    _, messages = store.auto_research_message_history(episode.episode_id, limit=10)
    assert len(messages) == 1
    assert messages[0].delivery_operation_id == retry.operation_id
    store.request_agent_task_pause(retry.operation_id)
    assert store.episode(episode.episode_id) == before


def test_child_artifact_edits_do_not_change_parent_quiescence_or_report_snapshot(tmp_path):
    import logging

    from rcp.runs.auto_research import auto_research_exhaustion_signal, auto_research_wrapup_spec
    from rcp.runs.episodes.reconcile import EpisodeReconciler
    from rcp.runs.episodes.wrapup import begin_episode_report_wrapup

    from .test_child_experiment_parent_settlement import _parent_with_child

    store, parent, root, child = _parent_with_child(tmp_path)
    store.checkpoint_agent_task(
        root.operation_id,
        native_session_id="root-session",
        stage_root=str(tmp_path / "root-stage"),
    )
    store.complete_agent_task(child.operation_id, applied_revision=None, result={})
    signal = auto_research_exhaustion_signal(store, parent.episode_id)
    begin_episode_report_wrapup(store, auto_research_wrapup_spec(store, signal))
    edit = _task(
        store,
        "child-edit",
        kind="artifact_edit",
        graph_target=child.graph_target,
        native_session_id="child-session",
        stage_root=str(tmp_path),
        request={
            "provider": "codex",
            "artifact_edit": {"episode_id": child.episode_id, "operation_id": "child-edit"},
        },
    )
    store.create_artifact_edit_task(edit)
    reconciler = EpisodeReconciler(store, None, logger=logging.getLogger(__name__))
    assert store.auto_research_is_quiescent(parent.episode_id)
    assert not reconciler._has_unsettled_visible_episode_task(parent.episode_id)
    assert not store.auto_research_report_has_later_child_work(parent.episode_id)
    store.fail_agent_task(edit.operation_id, "Edit failed")
    assert store.auto_research_is_quiescent(parent.episode_id)
    assert not store.auto_research_report_has_later_child_work(parent.episode_id)
    assert store.auto_research_lifecycle_notices(parent.episode_id) == []
