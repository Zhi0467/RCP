"""Representative synthetic team data owned only by the disposable guest drive."""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import uuid
from pathlib import Path

from rcp.api import create_app
from rcp.attachments import ChatAttachmentStore
from rcp.config import AGENT_EXECUTION_PROFILES
from rcp.core.models import AuthorizedHuman, Patch
from rcp.history import HistoryManager
from rcp.projects import inspect_backup_project_registration
from rcp.providers import configured_runtime_id
from rcp.server_ops.backup_checkout import verify_checkout_identities
from rcp.server_ops.github import parse_github_repository_ref
from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.storage import (
    AgentTaskRecord,
    AppStore,
    ProjectProvisioningGitCheckRecord,
    ProjectProvisioningMachineIntent,
    ProjectProvisioningProviderCheckRecord,
    ProjectProvisioningProviderIntent,
    ProjectProvisioningRepositoryIntent,
)


def prepare_data(
    data_dir: Path,
    projects_root: Path,
    *,
    account: str = "rcp",
    bootstrap_code: str | None = None,
) -> dict:
    previous = os.umask(0o077)
    try:
        return _prepare_data(
            data_dir, projects_root, account=account, bootstrap_code=bootstrap_code
        )
    finally:
        os.umask(previous)


def _prepare_data(
    data_dir: Path, projects_root: Path, *, account: str, bootstrap_code: str | None
) -> dict:
    """Use application owners to build one completed, fully capturable project."""
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    projects_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if bootstrap_code is None:
        store, code = AppStore.initialize_team_space(
            data_dir / "rcp.sqlite3", "Reboot qualification"
        )
    else:
        store, code = AppStore(data_dir / "rcp.sqlite3"), bootstrap_code
    member, token = store.enroll_team_member(code, "Qualification researcher")
    authority = AuthorizedHuman(
        space_id=store.space_id, user_id=member.user_id, display_name=member.display_name
    )
    repository_ref = parse_github_repository_ref("git@github.com:Zhi0467/RCP.git")
    request = store.create_project_provisioning_request(
        kind="create_team_project",
        authorized_by=authority,
        name="Reboot qualification project",
        state_repository="paper",
        project_truth_scope=["paper"],
        default_run_truth_scope=["paper"],
        machines=[
            ProjectProvisioningMachineIntent(
                alias="server",
                location="local",
                os_account=account,
                central_root=str(projects_root),
            )
        ],
        repositories=[
            ProjectProvisioningRepositoryIntent(
                alias="paper", repository=repository_ref, machine_alias="server"
            )
        ],
        provider_checks=[
            ProjectProvisioningProviderIntent(
                profile=profile,
                provider="codex",
                runtime_id="codex:exec",
                model="qualification",
                reasoning="medium",
                machine_alias="server",
            )
            for profile in AGENT_EXECUTION_PROFILES
        ],
    )
    running = store.transition_project_provisioning_request(
        request.request_id,
        receipt_id="qualification-start",
        phase="provisioning_start",
        expected_revision=request.revision,
        expected_status="waiting_for_server_setup",
        to_status="setup_in_progress",
        machines=request.machines,
        repositories=request.repositories,
        provider_checks=request.provider_checks,
    )
    repository = projects_root / running.proposed_project_id / "repositories" / "paper"
    repository.mkdir(parents=True)

    def git(*arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.name", "RCP qualification")
    git("config", "user.email", "qualification@example.invalid")
    (repository / "README.md").write_text("Synthetic disposable recovery fixture.\n")
    git("add", "README.md")
    git("commit", "-q", "-m", "Create disposable fixture")
    git("remote", "add", "origin", repository_ref.ssh_clone_url)
    git("config", "remote.origin.pushurl", repository_ref.ssh_clone_url)
    commit = git("rev-parse", "HEAD")
    checked_at = store.now()
    machines = [
        running.machines[0].model_copy(update={"resolved_central_root": str(projects_root)})
    ]
    repositories = [
        running.repositories[0].model_copy(
            update={
                "resolved_path": str(repository),
                "checkout_disposition": "request_created",
                "git_check": ProjectProvisioningGitCheckRecord(
                    status="ready",
                    commit=commit,
                    write_verified=True,
                    deploy_key_label=f"rcp:{store.space_id}:{running.proposed_project_id}:paper",
                    public_key_fingerprint="SHA256:" + "A" * 43,
                    checked_at=checked_at,
                ),
            }
        )
    ]
    providers = [
        ProjectProvisioningProviderCheckRecord(
            **check.model_dump(
                mode="json",
                exclude={
                    "status",
                    "binary_path",
                    "version",
                    "resolved_runtime_id",
                    "execution_account",
                    "checked_at",
                    "diagnostic",
                },
            ),
            status="ready",
            binary_path="/bin/true",
            version="qualification",
            resolved_runtime_id=configured_runtime_id("codex", "exec"),
            execution_account=account,
            checked_at=checked_at,
        )
        for check in running.provider_checks
    ]
    ready = store.transition_project_provisioning_request(
        running.request_id,
        receipt_id="qualification-ready",
        phase="provisioning_review",
        expected_revision=running.revision,
        expected_status="setup_in_progress",
        to_status="ready_for_review",
        machines=machines,
        repositories=repositories,
        provider_checks=providers,
    )
    app = create_app(data_dir=data_dir)
    app.state.setup.create_prepared_team_project(ready, seat_member=member.user_id)
    completed = store.transition_project_provisioning_request(
        ready.request_id,
        receipt_id="qualification-completed",
        phase="member_finalize",
        expected_revision=ready.revision,
        expected_status="ready_for_review",
        to_status="completed",
        machines=ready.machines,
        repositories=ready.repositories,
        provider_checks=ready.provider_checks,
    )
    project = store.project(completed.proposed_project_id)
    assert project is not None
    registration = inspect_backup_project_registration(
        project,
        data_dir=data_dir,
        provisioning_requests=store.completed_project_provisioning_requests(project.project_id),
    )
    verify_checkout_identities(registration.recovery)
    assert registration.workspace.backup_canonical_source_plan().complete
    history = HistoryManager(registration.manifest)
    history.append(
        Patch(
            kind="refresh",
            author="agent",
            summary="Record the disposable recovery experiment.",
            run_truth_scope=["paper"],
            repositories_read=["paper"],
            ops=[
                {
                    "op": "create_nodes",
                    "nodes": [
                        {
                            "id": "exp/recovery",
                            "type": "experiment",
                            "title": "Reboot recovery",
                            "objective": "Retain canonical history through deployment recovery.",
                        }
                    ],
                }
            ],
        )
    )
    operation_id = str(uuid.uuid4())
    chat_id = str(uuid.uuid4())
    stage = data_dir / "run-stage" / operation_id
    stage.mkdir(parents=True, mode=0o700)
    (stage / "retained.txt").write_bytes(b"Retained paused Work scratch.\n")
    run = RunRequest(
        provider="codex",
        model="qualification",
        reasoning="medium",
        run_on="server",
        run_truth_scope=["paper"],
        chat_scope="project",
        chat_id=chat_id,
        message="Retain this paused scratch for recovery.",
        mode="work",
        patch_kind="work",
    )
    store.create_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id=project.project_id,
            kind="project_chat",
            status="paused",
            request=run.model_dump(mode="json"),
            created_at=store.now(),
            updated_at=store.now(),
            status_message="Paused fixture",
            authorized_by=authority,
            dispatch_authority=resolve_dispatch_authority("project_chat", run),
            stage_host="",
            stage_root=str(stage),
        )
    )
    attachment = ChatAttachmentStore(data_dir / "chat-attachments").add(
        project_id=project.project_id,
        chat_id=chat_id,
        client_id=str(uuid.uuid4()),
        filename="notes.txt",
        media_type="text/plain",
        source=io.BytesIO(b"Retained attachment before deployment.\n"),
    )
    return {
        "space_id": store.space_id,
        "member_id": member.user_id,
        "token": token,
        "project_id": project.project_id,
        "research": str(registration.manifest.research_dir),
        "canonical_patches": file_digests(history.patches_dir),
        "stage": str(stage),
        "attachment_id": attachment.attachment.attachment_id,
        "ledger_head": store.storage_schema_ledger_head(),
    }


def file_digests(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
