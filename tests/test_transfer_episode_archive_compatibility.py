from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from rcp.core.transition_models import GraphHeadRef
from rcp.storage import AppStore, EpisodeRecord
from rcp.transfer.records import TRANSFER_RECORD_SCHEMA_VERSION

from .test_project_transfer_request_storage import (
    _linked_pair,
    _ready_incoming,
    _source_configuration,
)


def _ended_episode(store, project_id, actor):
    now = store.now()
    episode_id = str(uuid.uuid4())
    store.seat_project_member(project_id, actor.user_id)
    store.create_episode(
        EpisodeRecord(
            episode_id=episode_id,
            project_id=project_id,
            mode="experiment_loop",
            control_node_id="retained-experiment",
            status="queued",
            invocation_ceiling=1,
            authorized_by=actor,
            created_at=now,
            updated_at=now,
        )
    )
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO experiment_episode_state (episode_id, created_at, updated_at) "
            "VALUES (?, ?, ?)",
            (episode_id, now, now),
        )
    store.end_episode_without_report(episode_id, ending="failed")
    return episode_id


def _admit(source, target, source_request, target_request, target_actor):
    _ready_incoming(target, target_request.request_id)
    target_request = target.record_target_project_transfer_admission(
        target_request.request_id, admitted_by=target_actor
    )
    source.accept_target_project_transfer_admission(
        source_request.request_id, receipt=target_request.target_admission_receipt
    )


def test_target_validates_record_capability_instead_of_echoing_unknown_version(tmp_path):
    with pytest.raises(ValueError):
        _linked_pair(
            tmp_path,
            configuration=_source_configuration(
                record_schema_version=TRANSFER_RECORD_SCHEMA_VERSION + 1
            ),
        )
    target = AppStore(tmp_path / "team" / "rcp.sqlite3")
    assert target.project_transfer_requests() == []


@pytest.mark.parametrize("archived", [False, True])
def test_source_release_freezes_shared_archive_preference(tmp_path: Path, archived: bool):
    source, target, actor, target_actor, configuration, request, incoming = _linked_pair(tmp_path)
    episode_id = _ended_episode(source, request.project_id, actor)
    if archived:
        source.set_episode_archived(request.project_id, episode_id, actor.user_id, archived=True)
    _admit(source, target, request, incoming, target_actor)
    released = source.record_source_project_transfer_release(
        request.request_id,
        released_by=actor,
        revalidated_configuration=configuration,
        source_head=GraphHeadRef(revision=0),
    )
    assert released.source_release_receipt is not None
    with pytest.raises(ValueError, match="moving to its admitted team space"):
        source.set_episode_archived(
            request.project_id, episode_id, actor.user_id, archived=not archived
        )
    assert source.episode_archive_states(request.project_id)[episode_id].archived == archived
