from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from rcp.providers import classify_terminal_error
from rcp.storage import AppStore

NOW = datetime(2026, 11, 14, tzinfo=UTC)
OLD = (NOW - timedelta(days=31)).isoformat()
RECENT = (NOW - timedelta(days=29)).isoformat()
SESSION = "019a0000-0000-7000-8000-000000000001"


def _insert(store: AppStore, table: str, **values: object) -> None:
    with store.connection() as connection:
        connection.execute(
            f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
            tuple(values.values()),
        )


def _run(
    store: AppStore,
    operation_id: str,
    session: str | None = SESSION,
    *,
    status: str = "succeeded",
    updated_at: str = OLD,
    episode_id: str | None = None,
    **request: object,
) -> None:
    _insert(
        store,
        "graph_runs",
        operation_id=operation_id,
        project_id="p",
        episode_id=episode_id,
        kind="project_chat",
        status=status,
        request_json=json.dumps({"provider": "codex", "chat_id": "chat", **request}),
        created_at=updated_at,
        updated_at=updated_at,
        status_message="",
        native_session_id=session,
        stage_machine_id="backend",
        stage_root="/stage",
    )


def _eligible(store: AppStore) -> list[str]:
    return [target.session_id for target in store.provider_session_sweep_targets(NOW.isoformat())]


def _episode(store: AppStore, episode_id: str, status: str, wrapup_state: str) -> None:
    _insert(
        store,
        "episodes",
        episode_id=episode_id,
        project_id="p",
        mode="experiment_loop",
        status=status,
        wrapup_state=wrapup_state,
        invocation_ceiling=1,
        created_at=OLD,
        updated_at=OLD,
    )


@pytest.fixture
def store(tmp_path) -> AppStore:
    return AppStore(tmp_path / "app.sqlite3")


def test_an_idle_session_is_eligible_after_retention_and_not_before(store):
    _run(store, "old")
    _run(store, "recent", "019a0000-0000-7000-8000-000000000002", updated_at=RECENT)
    _run(store, "unrecorded", None)
    assert _eligible(store) == [SESSION]


def test_the_newest_row_naming_a_session_ages_it(store):
    _run(store, "old")
    _run(store, "fork-turn", "019a0000-0000-7000-8000-000000000009", updated_at=RECENT)
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET request_json = json_set(request_json, '$.fork_session_id', ?) "
            "WHERE operation_id = 'fork-turn'",
            (SESSION,),
        )
    assert _eligible(store) == []


@pytest.mark.parametrize("status", ["queued", "running", "pausing", "paused"])
def test_a_live_row_holds_its_session(store, status):
    _run(store, "old")
    _run(store, "live", status=status)
    assert _eligible(store) == []


def test_a_live_fork_turn_holds_its_source(store):
    _run(store, "old")
    _run(store, "fork", "019a0000-0000-7000-8000-000000000003", status="running")
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET request_json = json_set(request_json, '$.fork_session_id', ?) "
            "WHERE operation_id = 'fork'",
            (SESSION,),
        )
    assert SESSION not in _eligible(store)


def test_an_unlaunched_branch_holds_its_source_turn_session(store):
    _run(store, "source-turn")
    record = {
        "project_id": "p",
        "chat_id": "branched",
        "graph_target": {"kind": "main", "branch_id": None},
        "kind": "project_chat",
        "node_id": None,
        "created_at": OLD,
        "origin": {"source_operation_id": "source-turn"},
    }
    _insert(store, "chat_forks", project_id="p", chat_id="branched", record_json=json.dumps(record))
    assert _eligible(store) == []
    _run(store, "branched-turn", "019a0000-0000-7000-8000-000000000004", chat_id="branched")
    assert SESSION in _eligible(store)


def test_a_detached_branch_does_not_hold(store):
    _run(store, "source-turn")
    record = {"origin": {"source_operation_id": "source-turn"}, "detached": True}
    _insert(store, "chat_forks", project_id="p", chat_id="branched", record_json=json.dumps(record))
    assert _eligible(store) == [SESSION]


@pytest.mark.parametrize(("state", "held"), [("pending", True), ("answered", False)])
def test_a_pending_question_holds_its_origin_session(store, state, held):
    _run(store, "old")
    _insert(
        store,
        "questions",
        question_id="q",
        project_id="p",
        owner_kind="chat",
        owner_id="chat",
        key="k",
        origin_json=json.dumps({"native_session_id": SESSION}),
        argument_hash="h",
        question="?",
        choices_json="[]",
        multiple=0,
        state=state,
        created_at=OLD,
    )
    assert (_eligible(store) == []) is held


def test_a_writing_session_holds(store):
    _run(store, "old")
    _insert(
        store,
        "writing_sessions",
        native_session_id=SESSION,
        provider="codex",
        machine_id="backend",
        project_id="p",
        model="m",
        created_at=OLD,
        last_resumed_at=OLD,
        introduction_hash_examined="",
        graph_revision_examined=0,
        research_md_hash_examined="",
    )
    assert _eligible(store) == []


@pytest.mark.parametrize(
    ("state", "held"), [("pending", True), ("outdated", True), ("applied", False)]
)
def test_an_open_paper_change_group_holds_its_coach_session(store, state, held):
    _run(store, "coach")
    _insert(
        store,
        "paper_change_groups",
        project_id="p",
        group_id="g",
        task_id="coach",
        base_revision=0,
        mode="review",
        state=state,
        created_at=OLD,
    )
    assert (_eligible(store) == []) is held


@pytest.mark.parametrize(
    ("status", "wrapup_state", "held"),
    [
        ("running", "not_started", True),
        ("needs_action", "not_started", True),
        ("completed", "pending", True),
        ("completed", "running", True),
        ("completed", "completed", False),
    ],
)
def test_an_open_episode_holds_its_sessions(store, status, wrapup_state, held):
    _episode(store, "e", status, wrapup_state)
    _run(store, "turn", episode_id="e")
    _run(store, "state-only", "019a0000-0000-7000-8000-000000000005")
    _insert(
        store,
        "experiment_episode_state",
        episode_id="e",
        native_session_id="019a0000-0000-7000-8000-000000000005",
        created_at=OLD,
        updated_at=OLD,
    )
    assert (_eligible(store) == []) is held


@pytest.mark.parametrize(("state", "held"), [("running", True), ("succeeded", False)])
def test_a_pending_wrapup_holds_its_session_after_its_episode_ends(store, state, held):
    _episode(store, "e", "completed", "completed")
    _run(store, "wrapup", "019a0000-0000-7000-8000-000000000006")
    _insert(
        store,
        "episode_wrapups",
        episode_id="e",
        partial=0,
        native_session_id="019a0000-0000-7000-8000-000000000006",
        receipt_json="{}",
        receipt_sha256="",
        state=state,
        created_at=OLD,
        updated_at=OLD,
    )
    assert (_eligible(store) == []) is held


def test_restore_only_ages_sessions_used_after_it(store):
    _run(store, "before-restore", updated_at=(NOW - timedelta(days=45)).isoformat())
    _run(
        store,
        "live-at-backup",
        "019a0000-0000-7000-8000-000000000006",
        status="running",
        updated_at=(NOW - timedelta(days=45)).isoformat(),
    )
    store.detach_restored_lifecycle(
        diagnostic="restored",
        confirmed_by="tester",
        detached_at=(NOW - timedelta(days=40)).isoformat(),
    )
    _run(store, "after-restore", "019a0000-0000-7000-8000-000000000007")
    assert _eligible(store) == ["019a0000-0000-7000-8000-000000000007"]


def test_marking_rechecks_the_rule_and_a_swept_session_is_dropped(store):
    _run(store, "old")
    _run(store, "other", "019a0000-0000-7000-8000-000000000008")
    targets = store.provider_session_sweep_targets(NOW.isoformat())
    _run(store, "late", status="running")
    marked = store.mark_provider_sessions_swept(targets, NOW.isoformat())
    assert [target.session_id for target in marked] == ["019a0000-0000-7000-8000-000000000008"]
    assert store.provider_session_swept("019a0000-0000-7000-8000-000000000008")
    assert not store.provider_session_swept(SESSION)
    pending = store.provider_session_sweep_targets(NOW.isoformat())
    assert [(target.session_id, target.marked) for target in pending] == [
        ("019a0000-0000-7000-8000-000000000008", True)
    ]
    store.record_provider_session_sweep_results([(pending[0], None)], NOW.isoformat())
    assert store.provider_session_sweep_targets(NOW.isoformat()) == []


@pytest.mark.parametrize(
    "text",
    [
        "thread/resume failed: no rollout found for thread id 019a (code -32600)",
        "No conversation found with session ID: 019a",
        "Error: Session not found",
    ],
)
def test_probed_missing_session_texts_are_stale_sessions(text):
    assert classify_terminal_error(text) == "stale_session"
