from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from rcp.service import RunRequest
from rcp.storage import (
    AppStore,
    ProjectRecord,
    ResultViewConflict,
    ResultViewRecord,
)

_VIEW_ID = "0123456789abcdef01234567"
_CREATED = datetime(2026, 8, 12, 1, 2, 3, tzinfo=UTC)


def _view(
    *,
    view_id: str = _VIEW_ID,
    project_id: str = "project-one",
    experiment_id: str = "experiment-one",
    chat_id: str = "chat-one",
    expires_at: datetime | None = None,
) -> ResultViewRecord:
    created_at = _CREATED.isoformat()
    return ResultViewRecord(
        view_id=view_id,
        project_id=project_id,
        experiment_id=experiment_id,
        chat_id=chat_id,
        origin_operation_id="operation-create",
        latest_operation_id="operation-create",
        provider="codex",
        model="",
        reasoning="high",
        run_on="local",
        native_session_id="native-session",
        stage_host="",
        stage_root="/tmp/rcp-run.chat-one",
        source_name="throughput-pilot.html",
        content_sha256="a" * 64,
        size_bytes=512,
        created_at=created_at,
        updated_at=created_at,
        expires_at=(expires_at or _CREATED + timedelta(days=7)).isoformat(),
    )


def _project(project_id: str) -> ProjectRecord:
    return ProjectRecord(
        project_id=project_id,
        locator=f"/tmp/{project_id}/research.yaml",
        name=project_id,
        state_location=f"/tmp/{project_id}/.research",
        state_remote=False,
        added_at=_CREATED.isoformat(),
    )


def test_result_view_request_is_a_strict_create_or_revise_union() -> None:
    create = RunRequest.model_validate(
        {
            "mode": "work",
            "chat_scope": "node",
            "node_id": "experiment-one",
            "result_view": {"action": "create"},
        }
    )
    revise = RunRequest.model_validate(
        {
            "mode": "work",
            "chat_scope": "node",
            "node_id": "experiment-one",
            "result_view": {"action": "revise", "view_id": _VIEW_ID},
        }
    )

    assert create.result_view is not None and create.result_view.action == "create"
    assert revise.result_view is not None and revise.result_view.action == "revise"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RunRequest.model_validate(
            {
                "mode": "work",
                "node_id": "experiment-one",
                "result_view": {"action": "create", "view_id": _VIEW_ID},
            }
        )
    with pytest.raises(ValidationError, match="Field required"):
        RunRequest.model_validate(
            {
                "mode": "work",
                "node_id": "experiment-one",
                "result_view": {"action": "revise"},
            }
        )
    with pytest.raises(ValidationError):
        RunRequest.model_validate(
            {
                "mode": "work",
                "node_id": "experiment-one",
                "result_view": {"action": "revise", "view_id": "not-an-opaque-id"},
            }
        )


@pytest.mark.parametrize(
    "values",
    [
        {"mode": "discuss", "chat_scope": "node", "node_id": "experiment-one"},
        {"mode": "work", "chat_scope": "project", "node_id": "experiment-one"},
        {"mode": "work", "chat_scope": "node", "node_id": None},
    ],
)
def test_result_view_request_requires_node_scoped_work(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="node-scoped Work"):
        RunRequest.model_validate({**values, "result_view": {"action": "create"}})


@pytest.mark.parametrize("legacy", [False, True])
def test_result_view_table_is_additive_and_contains_metadata_only(tmp_path, legacy: bool) -> None:
    path = tmp_path / "rcp.sqlite3"
    if legacy:
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE legacy_data (value TEXT NOT NULL)")
            connection.execute("INSERT INTO legacy_data(value) VALUES ('preserved')")

    AppStore(path)

    with sqlite3.connect(path) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(result_views)")]
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(result_views)").fetchall()
        }
        if legacy:
            assert connection.execute("SELECT value FROM legacy_data").fetchone() == ("preserved",)
    assert columns == [
        "view_id",
        "project_id",
        "experiment_id",
        "chat_id",
        "origin_operation_id",
        "latest_operation_id",
        "provider",
        "model",
        "reasoning",
        "run_on",
        "native_session_id",
        "stage_host",
        "stage_root",
        "source_name",
        "content_sha256",
        "size_bytes",
        "created_at",
        "updated_at",
        "expires_at",
        "kept_filename",
        "kept_at",
    ]
    assert "result_views_project_experiment" in indexes
    assert "result_views_project_chat" in indexes
    assert not {"bytes", "content", "html", "patch", "proposal", "revision"} & set(columns)


def test_result_view_insert_fetch_and_filtered_listing(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    first = store.create_result_view(_view())
    second = store.create_result_view(
        _view(
            view_id="1123456789abcdef01234567",
            experiment_id="experiment-two",
            chat_id="chat-two",
        )
    )

    assert store.result_view(first.view_id, as_of=_CREATED) == first
    assert {item.view_id for item in store.list_result_views("project-one", as_of=_CREATED)} == {
        first.view_id,
        second.view_id,
    }
    assert store.list_result_views(
        "project-one", experiment_id="experiment-one", as_of=_CREATED
    ) == [first]
    assert store.list_result_views("project-one", chat_id="chat-two", as_of=_CREATED) == [second]


def test_expired_temporary_view_is_hidden_but_available_for_diagnostics(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    record = store.create_result_view(_view(expires_at=_CREATED + timedelta(hours=1)))
    after_expiry = _CREATED + timedelta(hours=2)

    assert store.result_view(record.view_id, as_of=after_expiry) is None
    assert store.list_result_views(record.project_id, as_of=after_expiry) == []
    assert store.result_view_for_diagnostics(record.view_id) == record
    assert store.result_view(record.view_id, include_expired=True, as_of=after_expiry) == record


def test_kept_view_survives_expiry_and_keep_is_idempotent(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    record = store.create_result_view(_view(expires_at=_CREATED + timedelta(hours=1)))
    kept = store.mark_result_view_kept(
        record.view_id,
        expected_content_sha256=record.content_sha256,
        kept_filename="throughput-project-26-08-12.html",
        kept_at=(_CREATED + timedelta(minutes=1)).isoformat(),
    )
    retried = store.mark_result_view_kept(
        record.view_id,
        expected_content_sha256=record.content_sha256,
        kept_filename="must-not-replace-the-first-name.html",
        kept_at=(_CREATED + timedelta(minutes=2)).isoformat(),
    )

    assert retried == kept
    assert kept.kept_filename == "throughput-project-26-08-12.html"
    assert store.list_result_views(record.project_id, as_of=_CREATED + timedelta(days=30)) == [kept]


def test_active_chat_extends_only_unkept_view_retention(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    unkept = store.create_result_view(_view(expires_at=_CREATED + timedelta(hours=1)))
    kept_source = store.create_result_view(
        _view(
            view_id="2123456789abcdef01234567",
            expires_at=_CREATED + timedelta(hours=1),
        )
    )
    kept = store.mark_result_view_kept(
        kept_source.view_id,
        expected_content_sha256=kept_source.content_sha256,
        kept_filename="kept-project-26-08-12.html",
        kept_at=(_CREATED + timedelta(minutes=1)).isoformat(),
    )
    extended = _CREATED + timedelta(days=8)

    assert (
        store.refresh_result_view_expiry(
            unkept.project_id,
            unkept.chat_id,
            expires_at=extended.isoformat(),
            as_of=_CREATED,
        )
        == 1
    )
    assert (
        store.refresh_result_view_expiry(
            unkept.project_id,
            unkept.chat_id,
            expires_at=(_CREATED + timedelta(minutes=30)).isoformat(),
            as_of=_CREATED,
        )
        == 0
    )
    assert store.result_view_for_diagnostics(unkept.view_id).expires_at == extended.isoformat()
    assert store.result_view_for_diagnostics(kept.view_id).expires_at == kept.expires_at


def test_active_chat_cannot_revive_an_already_expired_unkept_view(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    created_at = datetime(2000, 1, 1, tzinfo=UTC)
    expired_at = created_at + timedelta(days=1)
    record = store.create_result_view(
        ResultViewRecord.model_validate(
            {
                **_view().model_dump(mode="python"),
                "created_at": created_at.isoformat(),
                "updated_at": created_at.isoformat(),
                "expires_at": expired_at.isoformat(),
            }
        )
    )

    refreshed = store.refresh_result_view_expiry(
        record.project_id,
        record.chat_id,
        expires_at=(_CREATED + timedelta(days=8)).isoformat(),
        as_of=_CREATED,
    )

    assert refreshed == 0
    unchanged = store.result_view_for_diagnostics(record.view_id)
    assert unchanged is not None
    assert unchanged.expires_at == expired_at.isoformat()
    assert store.result_view(record.view_id, as_of=_CREATED) is None


def test_revision_uses_digest_cas_and_preserves_view_identity(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    record = store.create_result_view(_view())
    updated_at = (_CREATED + timedelta(minutes=5)).isoformat()
    revised = store.revise_result_view(
        record.view_id,
        expected_content_sha256=record.content_sha256,
        latest_operation_id="operation-revise",
        content_sha256="b" * 64,
        size_bytes=640,
        updated_at=updated_at,
        expires_at=(_CREATED + timedelta(days=8)).isoformat(),
    )

    assert revised.view_id == record.view_id
    assert revised.origin_operation_id == record.origin_operation_id
    assert revised.latest_operation_id == "operation-revise"
    assert revised.content_sha256 == "b" * 64
    with pytest.raises(ResultViewConflict, match="changed before"):
        store.revise_result_view(
            record.view_id,
            expected_content_sha256=record.content_sha256,
            latest_operation_id="operation-stale",
            content_sha256="c" * 64,
            size_bytes=700,
            updated_at=(_CREATED + timedelta(minutes=6)).isoformat(),
            expires_at=(_CREATED + timedelta(days=8)).isoformat(),
        )
    assert store.result_view_for_diagnostics(record.view_id) == revised


def test_revision_after_keep_conflicts_without_changing_kept_metadata(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    record = store.create_result_view(_view())
    kept = store.mark_result_view_kept(
        record.view_id,
        expected_content_sha256=record.content_sha256,
        kept_filename="throughput-project-26-08-12.html",
        kept_at=(_CREATED + timedelta(minutes=1)).isoformat(),
    )

    with pytest.raises(ResultViewConflict, match="kept result view"):
        store.revise_result_view(
            record.view_id,
            expected_content_sha256=record.content_sha256,
            latest_operation_id="operation-revise-after-keep",
            content_sha256="b" * 64,
            size_bytes=640,
            updated_at=(_CREATED + timedelta(minutes=2)).isoformat(),
            expires_at=(_CREATED + timedelta(days=8)).isoformat(),
        )

    assert store.result_view_for_diagnostics(record.view_id) == kept
    descriptor = store.result_view_descriptor(kept, as_of=_CREATED)
    assert descriptor.state == "kept"
    assert descriptor.kept_filename == kept.kept_filename
    assert descriptor.can_revise is False


def test_keep_after_revision_conflicts_without_exposing_stale_keep_metadata(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    record = store.create_result_view(_view())
    revised = store.revise_result_view(
        record.view_id,
        expected_content_sha256=record.content_sha256,
        latest_operation_id="operation-revise-before-keep",
        content_sha256="b" * 64,
        size_bytes=640,
        updated_at=(_CREATED + timedelta(minutes=1)).isoformat(),
        expires_at=(_CREATED + timedelta(days=8)).isoformat(),
    )

    with pytest.raises(ResultViewConflict, match="changed before Keep"):
        store.mark_result_view_kept(
            record.view_id,
            expected_content_sha256=record.content_sha256,
            kept_filename="stale-copy.html",
            kept_at=(_CREATED + timedelta(minutes=2)).isoformat(),
        )

    assert store.result_view_for_diagnostics(record.view_id) == revised
    descriptor = store.result_view_descriptor(revised, as_of=_CREATED)
    assert descriptor.state == "temporary"
    assert descriptor.kept_filename is None
    assert descriptor.kept_at is None
    assert descriptor.can_revise is True


def test_project_identity_migration_and_deletion_include_result_views(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    legacy_id = "legacy-project"
    canonical_id = str(uuid.uuid4())
    store.upsert_project(_project(legacy_id))
    record = store.create_result_view(_view(project_id=legacy_id))

    store.migrate_project_identity(legacy_id, canonical_id, store.space_id)

    migrated = store.result_view_for_diagnostics(record.view_id)
    assert migrated is not None and migrated.project_id == canonical_id
    counts = store.delete_project_records(canonical_id)
    assert counts["result_views"] == 1
    assert store.result_view_for_diagnostics(record.view_id) is None


def test_public_descriptor_exposes_no_private_binding_fields(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    record = store.create_result_view(_view())

    descriptor = store.result_view_descriptor(record, as_of=_CREATED)

    assert descriptor.model_dump() == {
        "view_id": record.view_id,
        "chat_id": record.chat_id,
        "experiment_id": record.experiment_id,
        "name": record.source_name,
        "media_type": "text/html",
        "state": "temporary",
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "expires_at": record.expires_at,
        "kept_filename": None,
        "kept_at": None,
        "can_revise": True,
    }
    private_fields = {
        "project_id",
        "origin_operation_id",
        "latest_operation_id",
        "provider",
        "model",
        "reasoning",
        "run_on",
        "native_session_id",
        "stage_host",
        "stage_root",
        "source_name",
        "content_sha256",
        "size_bytes",
    }
    assert not private_fields & descriptor.model_dump().keys()


@pytest.mark.parametrize("source_name", ["nested/view.html", "view.htm", "view.png"])
def test_result_view_record_requires_one_plain_html_source(source_name: str) -> None:
    with pytest.raises(ValidationError):
        ResultViewRecord.model_validate({**_view().model_dump(), "source_name": source_name})
