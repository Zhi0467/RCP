"""Member-authorized product requests for team-project preparation."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from rcp.api.dependencies import get_identity_access, get_store
from rcp.api.identity import IdentityAccess
from rcp.config import AgentExecutionProfile
from rcp.core.models import AuthorizedHuman
from rcp.providers import ProviderId
from rcp.server_ops.github import GitHubRepositoryRef, parse_github_repository_ref
from rcp.server_ops.layout import DEFAULT_SERVER_LAYOUT
from rcp.server_ops.models import ServerStep
from rcp.storage import (
    AppStore,
    ProjectProvisioningCancellationDisposition,
    ProjectProvisioningCheckStatus,
    ProjectProvisioningMachineIntent,
    ProjectProvisioningMachineRecord,
    ProjectProvisioningProviderCheckRecord,
    ProjectProvisioningProviderIntent,
    ProjectProvisioningRepositoryIntent,
    ProjectProvisioningRepositoryRecord,
    ProjectProvisioningRequestRecord,
    ProjectProvisioningStatus,
    SpaceKind,
)

router = APIRouter()

IdentityDependency = Annotated[IdentityAccess, Depends(get_identity_access)]
StoreDependency = Annotated[AppStore, Depends(get_store)]

ProjectCreationIntent = Literal[
    "use_existing_checkout_personally",
    "create_shared_team_project",
    "move_personal_project_to_team",
]

_STATUS_LABELS: dict[ProjectProvisioningStatus, str] = {
    "waiting_for_server_setup": "Waiting for server setup",
    "setup_in_progress": "Setup in progress",
    "operator_action_needed": "Operator action needed",
    "ready_for_review": "Ready for review",
    "completed": "Completed",
    "cancelled": "Cancelled",
}
_CHECK_LABELS: dict[ProjectProvisioningCheckStatus, str] = {
    "pending": "Waiting for setup",
    "checking": "Checking",
    "operator_action_needed": "Operator action needed",
    "ready": "Ready",
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProjectCreationIntentControl(_StrictModel):
    intent: ProjectCreationIntent
    eligible: bool
    preselected: bool
    primary_action_label: str
    required_fields: tuple[str, ...]
    pinned_source_project_id: str | None = None
    unavailable_reason: str | None = None


class ProjectCreationControl(_StrictModel):
    intents: tuple[ProjectCreationIntentControl, ...]
    requires_authenticated_member: bool


class ProjectProvisioningMachineRequest(_StrictModel):
    alias: str
    location: Literal["local", "ssh"]
    host: str = ""
    os_account: str
    central_root: str | None = None

    def intent(self) -> ProjectProvisioningMachineIntent:
        central_root = self.central_root
        if self.location == "local" and central_root is None:
            central_root = str(DEFAULT_SERVER_LAYOUT.projects_root)
        if central_root is None:
            raise ValueError(
                "An SSH provisioning machine requires one reviewed absolute central root."
            )
        return ProjectProvisioningMachineIntent(
            alias=self.alias,
            location=self.location,
            host=self.host,
            os_account=self.os_account,
            central_root=central_root,
        )


class ProjectProvisioningRepositoryRequest(_StrictModel):
    alias: str
    source: str
    machine_alias: str

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        parse_github_repository_ref(value)
        return value

    def intent(self) -> ProjectProvisioningRepositoryIntent:
        return ProjectProvisioningRepositoryIntent(
            alias=self.alias,
            repository=parse_github_repository_ref(self.source),
            machine_alias=self.machine_alias,
        )


class ProjectProvisioningCreateRequest(_StrictModel):
    machines: list[ProjectProvisioningMachineRequest] = Field(min_length=1, max_length=32)
    repositories: list[ProjectProvisioningRepositoryRequest] = Field(
        min_length=1,
        max_length=64,
    )
    provider_checks: list[ProjectProvisioningProviderIntent] = Field(
        min_length=1,
        max_length=32,
    )


class ProjectProvisioningMachineProjection(_StrictModel):
    alias: str
    location: Literal["local", "ssh"]
    host: str
    os_account: str
    intended_central_root: str
    resolved_central_root: str | None
    ready: bool
    status_label: str


class ProjectProvisioningRepositoryProjection(_StrictModel):
    alias: str
    repository: GitHubRepositoryRef
    https_clone_url: str
    ssh_clone_url: str
    settings_url: str
    machine_alias: str
    intended_path: str
    resolved_path: str | None
    status: ProjectProvisioningCheckStatus
    status_label: str
    ready: bool
    commit: str | None
    write_verified: bool
    deploy_key_label: str | None
    public_key_fingerprint: str | None
    checked_at: str | None
    diagnostic: str | None


class ProjectProvisioningProviderProjection(_StrictModel):
    profile: AgentExecutionProfile
    provider: ProviderId
    runtime_id: str
    model: str
    reasoning: str
    machine_alias: str
    status: ProjectProvisioningCheckStatus
    status_label: str
    ready: bool
    checked_at: str | None
    diagnostic: str | None


class ProjectProvisioningReadinessProjection(_StrictModel):
    machines_ready: int
    machines_total: int
    repositories_ready: int
    repositories_total: int
    providers_ready: int
    providers_total: int
    all_ready: bool


class ProjectProvisioningFinalReview(_StrictModel):
    digest: str
    proposed_project_id: str
    authorized_by: AuthorizedHuman
    ready_at: str


class ProjectProvisioningResponse(_StrictModel):
    request_id: str
    kind: Literal["create_team_project"]
    status: ProjectProvisioningStatus
    status_label: str
    next_action: str | None
    can_run_setup: bool
    can_review: bool
    can_cancel: bool
    target_space_id: str
    proposed_project_id: str
    authorized_by: AuthorizedHuman
    machines: list[ProjectProvisioningMachineProjection]
    repositories: list[ProjectProvisioningRepositoryProjection]
    provider_checks: list[ProjectProvisioningProviderProjection]
    readiness: ProjectProvisioningReadinessProjection
    diagnostic: str | None
    operator_action: ServerStep | None
    operator_argv: tuple[str, ...]
    final_review: ProjectProvisioningFinalReview | None
    cancellation_disposition: ProjectProvisioningCancellationDisposition | None
    revision: int
    created_at: str
    updated_at: str
    setup_started_at: str | None
    completed_at: str | None
    cancelled_at: str | None


def project_creation_control(space_kind: SpaceKind) -> ProjectCreationControl:
    personal = space_kind == "personal"
    return ProjectCreationControl(
        requires_authenticated_member=not personal,
        intents=(
            ProjectCreationIntentControl(
                intent="use_existing_checkout_personally",
                eligible=personal,
                preselected=personal,
                primary_action_label="Use existing checkout",
                required_fields=(
                    "name",
                    "repositories",
                    "state_repository",
                    "execution",
                    "confirmed",
                ),
                unavailable_reason=(
                    None if personal else "Existing-checkout setup belongs to a personal space."
                ),
            ),
            ProjectCreationIntentControl(
                intent="create_shared_team_project",
                eligible=not personal,
                preselected=not personal,
                primary_action_label="Create shared team project",
                required_fields=("machines", "repositories", "provider_checks"),
                unavailable_reason=(
                    "Connect to a team space to create a shared project." if personal else None
                ),
            ),
            ProjectCreationIntentControl(
                intent="move_personal_project_to_team",
                eligible=False,
                preselected=False,
                primary_action_label="Move to team space",
                required_fields=(),
                unavailable_reason=("Personal-to-team transfer is not available in this build."),
            ),
        ),
    )


@router.post(
    "/api/project-provisioning/requests",
    response_model=ProjectProvisioningResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_project_provisioning_request(
    body: ProjectProvisioningCreateRequest,
    request: Request,
    *,
    identity_access: IdentityDependency,
    store: StoreDependency,
) -> ProjectProvisioningResponse:
    identity_access.require_team_space()
    authorized_by = identity_access.require_patch_capable_identity(request)
    try:
        record = store.create_project_provisioning_request(
            kind="create_team_project",
            authorized_by=authorized_by,
            machines=[machine.intent() for machine in body.machines],
            repositories=[repository.intent() for repository in body.repositories],
            provider_checks=body.provider_checks,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _project_provisioning_response(record, viewer_user_id=authorized_by.user_id)


@router.get(
    "/api/project-provisioning/requests",
    response_model=list[ProjectProvisioningResponse],
)
def project_provisioning_requests(
    request: Request,
    *,
    identity_access: IdentityDependency,
    store: StoreDependency,
) -> list[ProjectProvisioningResponse]:
    identity_access.require_team_space()
    viewer = identity_access.acting_user(request)
    return [
        _project_provisioning_response(record, viewer_user_id=viewer.user_id)
        for record in store.project_provisioning_requests()
        if record.kind == "create_team_project"
    ]


@router.get(
    "/api/project-provisioning/requests/{request_id}",
    response_model=ProjectProvisioningResponse,
)
def project_provisioning_request(
    request_id: str,
    request: Request,
    *,
    identity_access: IdentityDependency,
    store: StoreDependency,
) -> ProjectProvisioningResponse:
    identity_access.require_team_space()
    viewer = identity_access.acting_user(request)
    record = _request_or_404(store, request_id)
    if record.kind != "create_team_project":
        raise HTTPException(status_code=404, detail="Provisioning request not found")
    return _project_provisioning_response(record, viewer_user_id=viewer.user_id)


@router.post(
    "/api/project-provisioning/requests/{request_id}/cancel",
    response_model=ProjectProvisioningResponse,
)
def cancel_project_provisioning_request(
    request_id: str,
    request: Request,
    *,
    identity_access: IdentityDependency,
    store: StoreDependency,
) -> ProjectProvisioningResponse:
    identity_access.require_team_space()
    viewer = identity_access.acting_user(request)
    record = _request_or_404(store, request_id)
    if record.kind != "create_team_project":
        raise HTTPException(status_code=404, detail="Provisioning request not found")
    if record.authorized_by.user_id != viewer.user_id:
        raise HTTPException(
            status_code=403,
            detail="Only the member who authorized this preparation may cancel it.",
        )
    if record.status == "cancelled":
        return _project_provisioning_response(record, viewer_user_id=viewer.user_id)
    if record.status != "waiting_for_server_setup":
        raise HTTPException(
            status_code=409,
            detail=(
                "Server preparation has started. Its machine-owned setup flow must first "
                "record the exact cleanup or reuse disposition."
            ),
        )
    try:
        cancelled = store.transition_project_provisioning_request(
            request_id,
            receipt_id=f"member-cancel-{record.revision}",
            phase="member_cancel",
            expected_revision=record.revision,
            expected_status=record.status,
            to_status="cancelled",
            machines=record.machines,
            repositories=record.repositories,
            provider_checks=record.provider_checks,
            cancellation_disposition="nothing_to_remove",
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail="The provisioning request changed; reload it before cancelling.",
        ) from exc
    return _project_provisioning_response(cancelled, viewer_user_id=viewer.user_id)


def _request_or_404(store: AppStore, request_id: str) -> ProjectProvisioningRequestRecord:
    try:
        record = store.project_provisioning_request(request_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Provisioning request not found") from exc
    if record is None:
        raise HTTPException(status_code=404, detail="Provisioning request not found")
    return record


def _project_provisioning_response(
    record: ProjectProvisioningRequestRecord,
    *,
    viewer_user_id: str,
) -> ProjectProvisioningResponse:
    if record.kind != "create_team_project":
        raise ValueError("the new-team provisioning projection requires a create request")
    machines = [_machine_projection(machine) for machine in record.machines]
    repositories = [_repository_projection(repository) for repository in record.repositories]
    providers = [_provider_projection(check) for check in record.provider_checks]
    readiness = ProjectProvisioningReadinessProjection(
        machines_ready=sum(machine.ready for machine in machines),
        machines_total=len(machines),
        repositories_ready=sum(repository.ready for repository in repositories),
        repositories_total=len(repositories),
        providers_ready=sum(provider.ready for provider in providers),
        providers_total=len(providers),
        all_ready=all(machine.ready for machine in machines)
        and all(repository.ready for repository in repositories)
        and all(provider.ready for provider in providers),
    )
    return ProjectProvisioningResponse(
        request_id=record.request_id,
        kind=record.kind,
        status=record.status,
        status_label=_STATUS_LABELS[record.status],
        next_action=_next_action(record),
        can_run_setup=record.status
        in {"waiting_for_server_setup", "setup_in_progress", "operator_action_needed"},
        can_review=record.status == "ready_for_review",
        can_cancel=(
            record.status == "waiting_for_server_setup"
            and record.authorized_by.user_id == viewer_user_id
        ),
        target_space_id=record.target_space_id,
        proposed_project_id=record.proposed_project_id,
        authorized_by=record.authorized_by,
        machines=machines,
        repositories=repositories,
        provider_checks=providers,
        readiness=readiness,
        diagnostic=record.retryable_diagnostic,
        operator_action=record.operator_action,
        operator_argv=(
            str(DEFAULT_SERVER_LAYOUT.cli_wrapper),
            "server",
            "project",
            "provision",
            record.request_id,
        ),
        final_review=(
            ProjectProvisioningFinalReview(
                digest=record.final_review_digest,
                proposed_project_id=record.proposed_project_id,
                authorized_by=record.authorized_by,
                ready_at=record.ready_at,
            )
            if record.final_review_digest is not None and record.ready_at is not None
            else None
        ),
        cancellation_disposition=record.cancellation_disposition,
        revision=record.revision,
        created_at=record.created_at,
        updated_at=record.updated_at,
        setup_started_at=record.setup_started_at,
        completed_at=record.completed_at,
        cancelled_at=record.cancelled_at,
    )


def _next_action(record: ProjectProvisioningRequestRecord) -> str | None:
    if record.status == "waiting_for_server_setup":
        return "Run server setup."
    if record.status == "setup_in_progress":
        return "Wait for server setup, or resume the same command after an interruption."
    if record.status == "operator_action_needed":
        assert record.operator_action is not None
        return record.operator_action.message
    if record.status == "ready_for_review":
        return "Review the prepared project."
    return None


def _machine_projection(
    machine: ProjectProvisioningMachineRecord,
) -> ProjectProvisioningMachineProjection:
    ready = machine.resolved_central_root is not None
    return ProjectProvisioningMachineProjection(
        alias=machine.alias,
        location=machine.location,
        host=machine.host,
        os_account=machine.os_account,
        intended_central_root=machine.central_root,
        resolved_central_root=machine.resolved_central_root,
        ready=ready,
        status_label="Ready" if ready else "Waiting for setup",
    )


def _repository_projection(
    repository: ProjectProvisioningRepositoryRecord,
) -> ProjectProvisioningRepositoryProjection:
    check = repository.git_check
    return ProjectProvisioningRepositoryProjection(
        alias=repository.alias,
        repository=repository.repository,
        https_clone_url=repository.repository.https_clone_url,
        ssh_clone_url=repository.repository.ssh_clone_url,
        settings_url=repository.repository.settings_url,
        machine_alias=repository.machine_alias,
        intended_path=repository.intended_path,
        resolved_path=repository.resolved_path,
        status=check.status,
        status_label=_CHECK_LABELS[check.status],
        ready=check.status == "ready",
        commit=check.commit,
        write_verified=check.write_verified,
        deploy_key_label=check.deploy_key_label,
        public_key_fingerprint=check.public_key_fingerprint,
        checked_at=check.checked_at,
        diagnostic=check.diagnostic,
    )


def _provider_projection(
    check: ProjectProvisioningProviderCheckRecord,
) -> ProjectProvisioningProviderProjection:
    return ProjectProvisioningProviderProjection(
        profile=check.profile,
        provider=check.provider,
        runtime_id=check.runtime_id,
        model=check.model,
        reasoning=check.reasoning,
        machine_alias=check.machine_alias,
        status=check.status,
        status_label=_CHECK_LABELS[check.status],
        ready=check.status == "ready",
        checked_at=check.checked_at,
        diagnostic=check.diagnostic,
    )


__all__ = [
    "ProjectCreationControl",
    "ProjectProvisioningCreateRequest",
    "ProjectProvisioningResponse",
    "project_creation_control",
    "router",
]
