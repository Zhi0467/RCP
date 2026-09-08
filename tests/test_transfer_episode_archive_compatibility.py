from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from rcp.core.transition_models import GraphHeadRef
from rcp.storage import AppStore, EpisodeRecord, ProjectTransferRepositorySource
from rcp.storage.provisioning import project_transfer_source_configuration_sha256

from .test_project_transfer_request_storage import (
    _actor,
    _linked_pair,
    _project,
    _ready_incoming,
    _source_configuration,
)


class _ProtocolThreeSourceConfiguration(BaseModel):
    """The shipped strict protocol-three target configuration boundary."""

    model_config = ConfigDict(extra="forbid")

    source_rcp_version: str
    source_schema_generation: int
    supported_archive_codecs: tuple[str, ...]
    machine_aliases: tuple[str, ...]
    repositories: tuple[ProjectTransferRepositorySource, ...]
    state_repository: str
    project_truth_scope: tuple[str, ...]
    default_run_truth_scope: tuple[str, ...]
    source_manifest_sha256: str


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


def test_archived_record_capability_refuses_previous_target_before_source_release(tmp_path):
    legacy = _source_configuration()
    previous = _ProtocolThreeSourceConfiguration.model_validate_json(legacy.model_dump_json())
    assert previous.model_dump_json() == legacy.model_dump_json()
    archived = _source_configuration(record_schema_version=2)
    assert project_transfer_source_configuration_sha256(archived) != (
        project_transfer_source_configuration_sha256(legacy)
    )
    source = AppStore(tmp_path / "personal" / "rcp.sqlite3")
    target = AppStore(tmp_path / "team" / "rcp.sqlite3", space_kind="team")
    actor = _actor(source, "Z")
    project_id = str(uuid.uuid4())
    _project(source, project_id)
    episode_id = _ended_episode(source, project_id, actor)
    source.set_episode_archived(project_id, episode_id, actor.user_id, archived=True)
    request = source.create_source_project_transfer_request(
        project_id=project_id,
        target_space_id=target.space_id,
        initiated_by=actor,
        source_configuration=archived,
    )
    with pytest.raises(ValidationError, match="record_schema_version"):
        _ProtocolThreeSourceConfiguration.model_validate_json(
            request.source_configuration.model_dump_json()
        )
    assert target.project_transfer_requests() == []
    current = source.project_transfer_request(request.request_id)
    assert current.source_release_receipt is None
    assert source.project(request.project_id).home_space_id == source.space_id


def test_target_validates_record_capability_instead_of_echoing_unknown_version(tmp_path):
    with pytest.raises(ValueError, match="does not support the source transfer record schema"):
        _linked_pair(tmp_path, configuration=_source_configuration(record_schema_version=3))
    target = AppStore(tmp_path / "team" / "rcp.sqlite3")
    source = AppStore(tmp_path / "personal" / "rcp.sqlite3")
    assert target.project_transfer_requests() == []
    [request] = source.project_transfer_requests()
    assert request.source_release_receipt is None


@pytest.mark.parametrize("unsettled", [False, True])
def test_archiving_after_preparation_is_rechecked_under_source_release_lock(tmp_path, unsettled):
    source, target, actor, target_actor, configuration, request, incoming = _linked_pair(tmp_path)
    _admit(source, target, request, incoming, target_actor)
    episode_id = _ended_episode(source, request.project_id, actor)
    source.set_episode_archived(request.project_id, episode_id, actor.user_id, archived=True)
    if unsettled:
        with source.connection() as connection:
            connection.execute(
                "UPDATE episodes SET status = 'running', ending = NULL, ended_at = NULL "
                "WHERE episode_id = ?",
                (episode_id,),
            )
        assert not source.episode_archive_states(request.project_id)[episode_id].archived
    assert source.project_transfer_record_schema_version(request.project_id) == 2

    with pytest.raises(ValueError, match="archive state changed after transfer preparation"):
        source.record_source_project_transfer_release(
            request.request_id,
            released_by=actor,
            revalidated_configuration=configuration,
            source_head=GraphHeadRef(revision=0),
        )
    assert source.project_transfer_request(request.request_id).source_release_receipt is None
    assert source.project(request.project_id).home_space_id == source.space_id


@pytest.mark.parametrize("archived", [False, True])
def test_source_release_freezes_shared_archive_preference(tmp_path: Path, archived: bool):
    source, target, actor, target_actor, configuration, request, incoming = _linked_pair(
        tmp_path, configuration=_source_configuration(record_schema_version=2 if archived else None)
    )
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
