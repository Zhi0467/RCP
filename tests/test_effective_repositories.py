from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from rcp.provisioning_repositories import effective_repositories
from rcp.server_ops.github import parse_github_repository_ref
from rcp.storage.models import ProjectProvisioningGitCheckRecord, ProjectRecord
from rcp.storage.provisioning import project_provisioning_review_digest
from tests.test_project_provisioning_storage import _create, _team_store


def test_effective_repositories_orders_completed_changes_and_keeps_latest_proof(tmp_path):
    store, human = _team_store(tmp_path)
    base = _create(store, human)
    now = datetime.now(UTC)

    def completed(kind, alias, source, index, truth=True):
        stamp = (now + timedelta(seconds=index)).isoformat()
        path = f"{base.machines[0].central_root}/{base.proposed_project_id}/repositories/{alias}"
        repository = base.repositories[0].model_copy(
            update={
                "alias": alias,
                "repository": source,
                "count_as_project_truth": truth,
                "intended_path": path,
                "resolved_path": path,
                "checkout_disposition": "request_created",
                "git_check": ProjectProvisioningGitCheckRecord(
                    status="ready",
                    commit=str(index + 1) * 40,
                    checked_at=stamp,
                    write_verified=source is not None,
                    deploy_key_label=(
                        f"rcp:{store.space_id}:{base.proposed_project_id}:{alias}"
                        if source is not None
                        else None
                    ),
                    public_key_fingerprint="SHA256:" + "A" * 43 if source is not None else None,
                ),
            }
        )
        record = base.model_copy(
            update={
                "request_id": str(uuid.uuid4()),
                "kind": kind,
                "status": "completed",
                "target_project_id": base.proposed_project_id if index else None,
                "repositories": [repository],
                "ready_at": stamp,
                "completed_at": stamp,
                "machines": [
                    base.machines[0].model_copy(
                        update={"resolved_central_root": base.machines[0].central_root}
                    )
                ],
                "provider_checks": [
                    base.provider_checks[0].model_copy(
                        update={"status": "ready", "checked_at": stamp}
                    )
                ],
                "name": None,
                "state_repository": None,
                "project_truth_scope": [],
                "default_run_truth_scope": [],
            }
        )
        record.final_review_digest = project_provisioning_review_digest(record)
        return type(record).model_validate(record.model_dump())

    create = completed("create_team_project", "paper", base.repositories[0].repository, 0)
    add = completed("add_repository", "code", None, 1, truth=False)
    connect = completed(
        "connect_repository",
        "code",
        parse_github_repository_ref("https://github.com/example/code"),
        2,
    )
    project = ProjectRecord(
        project_id=base.proposed_project_id,
        home_space_id=store.space_id,
        locator="/project/.research/manifest.toml",
        name="Project",
        state_location="/project/.research",
        state_remote=False,
        added_at=now.isoformat(),
    )
    assert effective_repositories(project, [create]) == tuple(create.repositories)
    assert effective_repositories(project, [add, create])[1].repository is None
    ignored = connect.model_copy(update={"target_space_id": str(uuid.uuid4())})
    pending = connect.model_copy(update={"status": "setup_in_progress"})
    result = effective_repositories(project, [connect, ignored, pending, create, add])
    assert [repository.alias for repository in result] == ["paper", "code"]
    assert result[1].repository == connect.repositories[0].repository
    assert result[1].git_check == connect.repositories[0].git_check
    assert result[1].count_as_project_truth is False
    with pytest.raises(ValueError, match="digest is stale"):
        effective_repositories(
            project, [create.model_copy(update={"final_review_digest": "0" * 64})]
        )
    with pytest.raises(ValueError, match="existing alias"):
        effective_repositories(project, [create, connect])
    rerouted = connect.model_copy(
        update={"machines": [connect.machines[0].model_copy(update={"os_account": "other"})]}
    )
    rerouted.final_review_digest = project_provisioning_review_digest(rerouted)
    with pytest.raises(ValueError, match="cannot replace a checkout"):
        effective_repositories(project, [create, add, rerouted])
