from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from rcp.runs.session_master import continuation_session_master, start_session_master
from rcp.storage import AgentTaskRecord, AppStore
from tests.test_auto_research_children_storage import _identity, _project


def _task(
    store: AppStore, operation_id: str, stage: Path, status: str = "succeeded"
) -> SimpleNamespace:
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id="project",
            kind="project_chat",
            status=status,  # type: ignore[arg-type]
            request={"chat_id": "chat"},
            created_at=now,
            updated_at=now,
            status_message="fixture",
            authorized_by=_identity(store),
        ),
        continuation_cause="fresh" if operation_id == "start" else "resume",
    )
    store.checkpoint_agent_task(operation_id, native_session_id="session", stage_root=str(stage))
    return SimpleNamespace(store=store, operation_id=operation_id)


def test_a_continuation_keeps_restores_or_replaces_its_session_master(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    stage = tmp_path / "stage"
    stage.mkdir()
    start = start_session_master(
        _task(store, "start", stage),  # type: ignore[arg-type]
        local_stage=stage,
        remote_stage=None,
        label_prefix="owner",
        key="key-1",
        content="master one",
        values={"patch": {"path": "/stage/patch.json"}},
    )
    Path(start.path).unlink()

    def continuation(operation_id: str, key: str, status: str = "succeeded") -> object:
        return continuation_session_master(
            _task(store, operation_id, stage, status),  # type: ignore[arg-type]
            local_stage=stage,
            remote_stage=None,
            native_session_id="session",
            label_prefix="owner",
            key=key,
            render=lambda: f"master for {key}",
        )

    kept = continuation("wake-1", "key-1")
    assert kept.path == start.path and not kept.bootstrap
    # The kept master reports the values it was rendered with, so only a change is sent.
    assert kept.values == {"patch": {"path": "/stage/patch.json"}}
    assert Path(kept.path).read_text(encoding="utf-8") == "master one"

    # A replacement recorded by an attempt that failed may never have been delivered.
    undelivered = continuation("wake-failed", "key-2", status="failed")
    assert undelivered.bootstrap
    replaced = continuation("wake-2", "key-2")
    assert replaced.bootstrap and replaced.replaces
    assert Path(replaced.path).read_text(encoding="utf-8") == "master for key-2"

    # The replacement is now what the session holds.
    after = continuation("wake-3", "key-2")
    assert after.path == replaced.path and not after.bootstrap
