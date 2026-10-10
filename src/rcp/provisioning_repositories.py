"""Effective team checkout provenance, shared by storage and project consumers."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from rcp.storage.models import (
    ProjectProvisioningRepositoryRecord,
    ProjectProvisioningRequestRecord,
    ProjectRecord,
)


def effective_repositories(
    project: ProjectRecord,
    provisioning_requests: Iterable[ProjectProvisioningRequestRecord],
) -> tuple[ProjectProvisioningRepositoryRecord, ...]:
    """Fold completed, reviewed requests for this exact project and home.

    The creation request establishes alias order; adds append and connects replace
    the source, deploy-key evidence, and checkout proof in place. In-flight work
    has no effect. This is operational provenance, never manifest/truth authority.
    Callers can pass the records of a database snapshot without opening live state.
    """
    from rcp.storage.provisioning import project_provisioning_review_digest

    if project.home_space_id is None:
        raise ValueError("The project has no durable home-space identity.")
    requests = [
        request
        for request in provisioning_requests
        if request.status == "completed"
        and request.proposed_project_id == project.project_id
        and request.target_space_id == project.home_space_id
    ]
    origins = [
        request
        for request in requests
        if request.kind in {"create_team_project", "incoming_transfer"}
    ]
    if len(origins) != 1:
        raise ValueError("The project requires exactly one completed creation request.")
    ordered = sorted(requests, key=_completion_order)
    if ordered[0] != origins[0]:
        raise ValueError("Repository changes cannot precede project creation.")
    repositories: dict[str, ProjectProvisioningRepositoryRecord] = {}
    checkout_machines: dict[str, tuple[str, str | None, str]] = {}
    for request in ordered:
        if project_provisioning_review_digest(request) != request.final_review_digest:
            raise ValueError("The completed provisioning review digest is stale.")
        if request.kind in {"add_repository", "connect_repository"} and (
            request.target_project_id != project.project_id or len(request.repositories) != 1
        ):
            raise ValueError("Repository changes must target this project and one repository.")
        machines = {machine.alias: machine for machine in request.machines}
        for repository in request.repositories:
            machine = machines[repository.machine_alias]
            machine_identity = (machine.location, machine.host, machine.os_account)
            if (
                repository.resolved_path is None
                or repository.git_check.status != "ready"
                or repository.git_check.commit is None
            ):
                raise ValueError("The completed repository checkout proof is incomplete.")
            previous = repositories.get(repository.alias)
            if request.kind == "connect_repository":
                if previous is None or repository.repository is None:
                    raise ValueError("Connecting requires an existing alias and GitHub source.")
                if (
                    previous.machine_alias != repository.machine_alias
                    or previous.resolved_path != repository.resolved_path
                    or checkout_machines[repository.alias] != machine_identity
                    or (
                        previous.repository is not None
                        and previous.repository != repository.repository
                    )
                ):
                    raise ValueError("Connecting cannot replace a checkout or GitHub identity.")
                repository = repository.model_copy(
                    update={"count_as_project_truth": previous.count_as_project_truth}
                )
            elif previous is not None:
                raise ValueError("A completed request repeats an existing repository alias.")
            repositories[repository.alias] = repository
            checkout_machines[repository.alias] = machine_identity
    identities = [
        repository.repository.identity
        for repository in repositories.values()
        if repository.repository is not None
    ]
    if len(identities) != len(set(identities)):
        raise ValueError("One GitHub repository cannot supply two project repositories.")
    return tuple(repositories.values())


def _completion_order(request: ProjectProvisioningRequestRecord) -> tuple[datetime, bool, str]:
    if request.completed_at is None:
        raise ValueError("The completed provisioning proof has no completion time.")
    completed_at = datetime.fromisoformat(request.completed_at)
    # Older store timestamps may omit an offset; those timestamps are UTC.
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=UTC)
    return (
        completed_at,
        request.kind not in {"create_team_project", "incoming_transfer"},
        request.request_id,
    )


if TYPE_CHECKING:
    from rcp.storage import AppStore


def team_repository_sources(
    store: AppStore, project_id: str
) -> dict[str, ProjectProvisioningRepositoryRecord] | None:
    """Return each team repository's effective record, or None when unknown.

    Readers use this to tell a proven server-only repository from one that has
    a GitHub source. A project whose provisioning evidence cannot be resolved,
    such as one created before provisioning requests, is unknown; callers keep
    today's GitHub behavior for it rather than treating it as server only.
    """

    if store.space_kind != "team":
        return None
    project = store.project(project_id)
    if project is None:
        return None
    try:
        records = effective_repositories(
            project, store.completed_project_provisioning_requests(project_id)
        )
    except ValueError:
        return None
    return {record.alias: record for record in records}


def is_server_only(
    sources: dict[str, ProjectProvisioningRepositoryRecord] | None, alias: str
) -> bool:
    """True only when the evidence proves the repository has no GitHub source."""

    record = (sources or {}).get(alias)
    return record is not None and record.repository is None
