from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from rcp.background import AgentTaskExecution
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.runs.session_master import (
    commit_prepared_graph_observation,
    merge_prepared_graph_observation_delta,
    prepare_graph_observation,
    prepared_graph_observation_delta,
)
from rcp.service import ProjectService
from rcp.storage import AppStore
from tests.test_auto_research_children_storage import _project
from tests.test_session_master import _task


class _Engine:
    def __init__(self, owners: list[str]) -> None:
        self.owners = owners

    def resolve(self, spec, *, default_target):
        return GraphHeadRef(target=spec.target or default_target, revision=spec.revision)

    def steps(self, head):
        return [
            SimpleNamespace(
                after_head=GraphHeadRef(target=head.target, revision=index),
                accepted=owner != "rejected",
                patch=SimpleNamespace(source_operation_id=owner, task_id=None),
            )
            for index, owner in enumerate(self.owners, start=1)
            if index <= head.revision
        ]


@pytest.mark.parametrize(
    ("owners", "expected"),
    [(["old", "start"], 2), (["old", "external", "start"], 1), (["old", "rejected", "start"], 3)],
)
def test_commit_observes_snapshot_and_only_uninterleaved_apply(tmp_path, owners, expected):
    store = AppStore(tmp_path / "app.sqlite3")
    _project(store)
    execution = cast(AgentTaskExecution, _task(store, "start", tmp_path))
    service = cast(ProjectService, SimpleNamespace(history_engine=_Engine(owners)))
    target = GraphTargetRef()
    prepare_graph_observation(
        execution,
        service,
        owner_id="chat:c",
        target=target,
        revision=1,
        native_session_id="session",
    )
    # Staging or a failed turn leaves the observation unknown.
    assert store.graph_seen_head("project", "chat:c", "session", target) is None
    assert prepared_graph_observation_delta(execution, "session") == {}
    commit_prepared_graph_observation(
        execution, service, native_session_id="session", applied_revision=len(owners)
    )
    seen = store.graph_seen_head("project", "chat:c", "session", target)
    assert seen is not None and seen.revision == expected
    # A repeated settlement must not regress the absorbed head.
    commit_prepared_graph_observation(execution, service, native_session_id="session")
    seen = store.graph_seen_head("project", "chat:c", "session", target)
    assert seen is not None and seen.revision == expected


def test_owner_session_target_separation_and_cas(tmp_path: Path):
    store = AppStore(tmp_path / "app.sqlite3")
    target = GraphTargetRef()
    head = GraphHeadRef(target=target, revision=4)
    store.commit_graph_seen_head(
        project_id="p",
        owner_id="chat:c",
        native_session_id="s",
        head=head,
        operation_id="first",
        expected_operation_id=None,
    )
    assert store.graph_seen_head("p", "chat:c", "s", target) == head
    for owner in ("episode:c", "auto-root:c", "auto-child:c"):
        assert store.graph_seen_head("p", owner, "s", target) is None
    assert store.graph_seen_head("p", "chat:c", "other", target) is None
    assert (
        store.graph_seen_head("p", "chat:c", "s", GraphTargetRef(kind="branch", branch_id="b"))
        is None
    )
    with pytest.raises(ValueError, match="compare-and-swap"):
        store.commit_graph_seen_head(
            project_id="p",
            owner_id="chat:c",
            native_session_id="s",
            head=head,
            operation_id="stale",
            expected_operation_id=None,
        )


def test_prepared_delta_is_turn_relative_and_unknown_is_not_unchanged(tmp_path):
    store = AppStore(tmp_path / "app.sqlite3")
    _project(store)
    service = cast(ProjectService, SimpleNamespace(history_engine=_Engine([])))
    target = GraphTargetRef()
    first = cast(AgentTaskExecution, _task(store, "start", tmp_path))
    prepare_graph_observation(
        first,
        service,
        owner_id="chat:c",
        target=target,
        revision=1,
        native_session_id="session",
    )
    commit_prepared_graph_observation(first, service, native_session_id="session")
    next_turn = cast(AgentTaskExecution, _task(store, "second", tmp_path))
    prepare_graph_observation(
        next_turn,
        service,
        owner_id="chat:c",
        target=target,
        revision=2,
        native_session_id="session",
    )
    assert prepared_graph_observation_delta(next_turn, "session") == {
        "current": {"graph_changed_since": "main@1", "graph_head": "main@2"}
    }
    delta = {"current": {"other": "retained"}, "browser": "unchanged"}
    assert merge_prepared_graph_observation_delta(delta, next_turn, "session") == {
        "current": {
            "other": "retained",
            "graph_changed_since": "main@1",
            "graph_head": "main@2",
        },
        "browser": "unchanged",
    }
    assert delta == {"current": {"other": "retained"}, "browser": "unchanged"}
    seen = store.graph_seen_head("project", "chat:c", "session", target)
    assert seen is not None and seen.revision == 1


def test_correction_observes_latest_snapshot_without_committing_failed_attempt(tmp_path):
    store = AppStore(tmp_path / "app.sqlite3")
    _project(store)
    execution = cast(AgentTaskExecution, _task(store, "start", tmp_path))
    service = cast(ProjectService, SimpleNamespace(history_engine=_Engine([])))
    target = GraphTargetRef()
    for revision in (1, 2):
        prepare_graph_observation(
            execution,
            service,
            owner_id="chat:c",
            target=target,
            revision=revision,
            native_session_id="session",
            previous_head=GraphHeadRef(revision=revision - 1) if revision > 1 else None,
        )
    assert prepared_graph_observation_delta(execution, "session") == {
        "current": {"graph_changed_since": "main@1", "graph_head": "main@2"}
    }
    assert store.graph_seen_head("project", "chat:c", "session", target) is None
    with pytest.raises(ValueError, match="binding"):
        prepare_graph_observation(
            execution,
            service,
            owner_id="episode:c",
            target=target,
            revision=2,
            native_session_id="session",
        )
    with pytest.raises(ValueError, match="session"):
        commit_prepared_graph_observation(execution, service, native_session_id="different")
    commit_prepared_graph_observation(execution, service, native_session_id="session")
    seen = store.graph_seen_head("project", "chat:c", "session", target)
    assert seen is not None and seen.revision == 2


def test_added_experiment_turns_keep_lineage_separate_from_shared_chat(tmp_path, monkeypatch):
    from rcp.runs.tasks.experiment_loop import _prepare_experiment_observation
    from rcp.runs.tasks.work_turn_runtime import WorkTurn

    store = AppStore(tmp_path / "app.sqlite3")
    _project(store)
    execution = cast(AgentTaskExecution, _task(store, "start", tmp_path))
    target = GraphTargetRef()
    service = cast(
        ProjectService,
        SimpleNamespace(history_engine=_Engine([]), history=SimpleNamespace(graph_target=target)),
    )
    store.commit_graph_seen_head(
        project_id="project",
        owner_id="chat:c",
        native_session_id="session",
        head=GraphHeadRef(revision=2),
        operation_id="human",
        expected_operation_id=None,
    )
    monkeypatch.setattr(
        store,
        "episode",
        lambda episode_id: SimpleNamespace(
            mode="experiment_loop",
            graph_target=target,
            continues_episode_id="root" if episode_id == "added" else None,
        ),
    )
    monkeypatch.setattr(
        store, "experiment_episode", lambda _id: SimpleNamespace(native_session_id="session")
    )
    turn = SimpleNamespace(
        execution=execution,
        service=service,
        context=SimpleNamespace(graph_revision=3),
        request=SimpleNamespace(control_episode_id="added", session_id="session"),
    )
    _prepare_experiment_observation(cast(WorkTurn, turn))
    assert prepared_graph_observation_delta(execution, "session") == {}
    commit_prepared_graph_observation(execution, service, native_session_id="session")
    loop_seen = store.graph_seen_head("project", "experiment:root", "session", target)
    chat_seen = store.graph_seen_head("project", "chat:c", "session", target)
    assert loop_seen is not None and loop_seen.revision == 3
    assert chat_seen is not None and chat_seen.revision == 2
    assert store.graph_seen_head("project", "experiment:added", "session", target) is None
