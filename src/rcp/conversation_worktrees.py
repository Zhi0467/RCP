"""Conversation-owned Git binding, admission, and controls.

Git is performed on the execution account; integration itself is an ordinary
human-authorized Work turn. No request can supply a filesystem root.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from rcp.agents.context import ChatContext
from rcp.agents.write_scope import (
    _ExecutionPathSemantics,
    _reject_broad_repository_root,
    _reject_repository_ownership_overlap,
)
from rcp.core.models import ConversationWorktreeBinding
from rcp.keyed_locks import KeyedLocks
from rcp.limits import WORKTREE_GIT_TIMEOUT_SECONDS
from rcp.service import ProjectService, RunRequest
from rcp.storage import AppStore
from rcp.transport import conversation_worktree
from rcp.transport.ssh import ssh_arguments

IntegrationChoice = Literal["pull_request", "starting_branch", "default_branch"]
# The process owns its data directory. Serialize human admission/removal for one
# chat through task creation; unrelated conversations retain independent locks.
conversation_worktree_locks = KeyedLocks()


class WorktreeIntegrationOption(BaseModel):
    id: IntegrationChoice
    label: str
    target_branch: str | None
    enabled: bool
    reason: str | None = None


class ConversationWorktreeResponse(BaseModel):
    show_chooser: bool = False
    can_choose: bool = False
    unavailable_reason: str | None = None
    binding: ConversationWorktreeBinding | None = None
    integration_options: list[WorktreeIntegrationOption] = Field(default_factory=list)
    can_remove: bool = False
    remove_reason: str | None = None
    ahead_count: int | None = None
    remote_branch_exists: bool | None = None
    remote_branch_evidence: str | None = None
    dirty_worktree: list[str] = Field(default_factory=list)


def worktree_command(
    store: AppStore,
    *,
    host: str,
    operation: str,
    binding: ConversationWorktreeBinding | None = None,
    **values: object,
) -> dict:
    payload = {
        "operation": operation,
        "timeout_seconds": WORKTREE_GIT_TIMEOUT_SECONDS,
        "require_owner": store.space_kind == "team",
        **({"binding": binding.model_dump(mode="json")} if binding else {}),
        **values,
    }
    try:
        if not host:
            return conversation_worktree.execute(payload)
        source = Path(conversation_worktree.__file__).read_text(encoding="utf-8")
        completed = subprocess.run(
            ssh_arguments(host, shlex.join(["python3", "-c", source, json.dumps(payload)])),
            capture_output=True,
            text=True,
            timeout=WORKTREE_GIT_TIMEOUT_SECONDS,
            check=False,
        )
        if completed.returncode:
            raise ValueError(completed.stderr.strip() or "Worktree execution failed")
        result = json.loads(completed.stdout)
        if not isinstance(result, dict):
            raise ValueError("Worktree execution returned an invalid response")
        if "error" in result:
            raise ValueError(str(result["error"]))
        return result
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"Worktree execution unavailable: {exc}") from exc


def _chat_busy(store: AppStore, project_id: str, chat_id: str) -> bool:
    return any(
        store.has_active_chat_task(project_id, kind, chat_id)
        or store.has_resumable_paused_chat_task(project_id, kind, chat_id)
        for kind in ("node_chat", "project_chat")
    )


def _repository(service: ProjectService, request: RunRequest):
    aliases = (
        request.run_truth_scope
        if request.run_truth_scope is not None
        else service.manifest.agent.default_run_truth_scope
    )
    if len(aliases) != 1:
        raise ValueError("Work in a worktree requires exactly one repository in the run scope.")
    alias = aliases[0]
    truth_scope = (
        service.history.state().project_truth_scope or service.manifest.project.truth_scope
    )
    if alias not in truth_scope:
        raise ValueError("The worktree repository is outside this project's truth scope.")
    repository = service.manifest.repository_map[alias]
    if repository.machine != request.run_on:
        raise ValueError("Run on the selected repository's machine to use a worktree.")
    return repository


def validate_worktree_binding(
    service: ProjectService,
    request: RunRequest,
    binding: ConversationWorktreeBinding,
    store: AppStore,
) -> None:
    repository = _repository(service, request)
    if (
        binding.chat_id != request.chat_id
        or binding.chat_scope != request.chat_scope
        or binding.node_id != request.node_id
        or binding.repository_alias != repository.alias
        or binding.machine != repository.machine
        or binding.execution_host != service.manifest.machine_map[repository.machine].host
    ):
        raise ValueError("The turn no longer matches its conversation worktree binding.")
    facts = worktree_command(
        store,
        host=binding.execution_host,
        operation="canonicalize",
        paths=[repository.path],
        shared_path=repository.path,
    )
    if facts["canonical"][repository.path] != binding.shared_path:
        raise ValueError("The registered repository moved after its worktree was bound.")
    if binding.status == "removed":
        raise ValueError(
            "This conversation's worktree was removed. Start a new chat to work again."
        )
    if binding.status == "removing":
        raise ValueError("Worktree removal is unfinished. Use Remove worktree to finish it.")


def _validate_creation_roots(
    service: ProjectService, store: AppStore, project_id: str, request: RunRequest, facts: dict
) -> None:
    repository = _repository(service, request)
    host = service.manifest.machine_map[repository.machine].host
    inventory = [
        item
        for item in service.repository_ownership_inventory(project_id=project_id)
        if item.execution_host == host
    ]
    owners = [
        item
        for item in inventory
        if item.project_id == project_id
        and item.alias == repository.alias
        and item.machine == repository.machine
        and item.path == repository.path
    ]
    if len(owners) != 1:
        raise ValueError("The worktree repository has no exact catalog owner.")
    resolved = worktree_command(
        store,
        host=host,
        operation="canonicalize",
        paths=[repository.path, facts["git_common_dir"], *[item.path for item in inventory]],
        shared_path=repository.path,
        prospective_worktree_path=facts["worktree_path"],
    )
    canonical = resolved["canonical"]
    if (
        canonical[repository.path] != facts["shared_path"]
        or canonical[facts["worktree_path"]] != facts["worktree_path"]
    ):
        raise ValueError("The planned worktree or repository moved before creation.")
    semantics = _ExecutionPathSemantics.for_execution(remote=bool(host))
    for root in (facts["shared_path"], facts["worktree_path"], facts["git_common_dir"]):
        _reject_broad_repository_root(
            root,
            account_home=resolved["account_home"],
            app_data_dir=store.path.parent if not host else None,
            path_semantics=semantics,
        )
        _reject_repository_ownership_overlap(
            repository=repository,
            project_id=project_id,
            admitted_owners={(project_id, repository.alias, repository.machine, repository.path)},
            registered_root=root,
            inventory=inventory,
            canonical_inventory=canonical,
            path_semantics=semantics,
        )


def admit_conversation_worktree(
    service: ProjectService, store: AppStore, project_id: str, request: RunRequest
) -> RunRequest:
    """Called under the chat admission lock before the durable task is created."""
    assert request.chat_id is not None
    binding = store.conversation_worktree(project_id, request.chat_id)
    if _chat_busy(store, project_id, request.chat_id):
        raise ValueError("This conversation already has an active or paused turn.")
    if request.worktree and request.mode != "work":
        raise ValueError("A worktree is created only by the first ticked Work turn.")
    if binding is None and request.worktree:
        if store.chat_has_work_turn(project_id, request.chat_id):
            raise ValueError(
                "Worktree selection is fixed by the first Work turn. Start a new chat."
            )
        repository = _repository(service, request)
        host = service.manifest.machine_map[repository.machine].host
        facts = worktree_command(
            store, host=host, operation="plan", shared_path=repository.path, chat_id=request.chat_id
        )
        if not host:
            destination = Path(facts["worktree_path"])
            data = store.path.parent.resolve()
            if destination == data or data in destination.parents or destination in data.parents:
                raise ValueError("A conversation worktree cannot be inside the RCP data directory.")
        _validate_creation_roots(service, store, project_id, request, facts)
        binding = store.create_conversation_worktree(
            ConversationWorktreeBinding(
                **facts,
                project_id=project_id,
                chat_scope=request.chat_scope,
                node_id=request.node_id,
                repository_alias=repository.alias,
                machine=repository.machine,
                execution_host=host,
            )
        )
    if binding is None:
        if request.worktree_integration:
            raise ValueError("Integration requires a bound conversation worktree.")
        return request
    validate_worktree_binding(service, request, binding, store)
    if binding.status == "creating":
        _validate_creation_roots(service, store, project_id, request, binding.model_dump())
        worktree_command(store, host=binding.execution_host, operation="create", binding=binding)
        binding = store.set_conversation_worktree_status(
            project_id, request.chat_id, expected_status="creating", status="ready"
        )
    else:
        worktree_command(store, host=binding.execution_host, operation="inspect", binding=binding)
    if request.worktree_integration:
        if request.mode != "work":
            raise ValueError("Integration is a Work turn.")
        facts = worktree_command(
            store, host=binding.execution_host, operation="inspect", binding=binding
        )
        option = _integration_option(binding, facts, request.worktree_integration)
        if option.target_branch is None and option.id != "pull_request":
            raise ValueError(option.reason or "The integration target is unavailable.")
        facts = worktree_command(
            store,
            host=binding.execution_host,
            operation="preflight",
            binding=binding,
            target_branch=option.target_branch,
        )
        return request.model_copy(
            update={
                "message": integration_instruction(binding, option, facts),
                "worktree_integration_target": option.target_branch,
            }
        )
    return request


def _integration_option(
    binding: ConversationWorktreeBinding, facts: dict, choice: IntegrationChoice
) -> WorktreeIntegrationOption:
    target = (
        binding.starting_branch
        if choice == "starting_branch"
        else facts["default_branch"]
        if choice == "default_branch"
        else None
    )
    reason = None
    if choice == "default_branch" and target is None:
        reason = "The default branch is unavailable; set origin/HEAD in the repository."
    label = (
        "Open a pull request"
        if choice == "pull_request"
        else f"Merge into {target or 'default branch'}"
    )
    return WorktreeIntegrationOption(
        id=choice, label=label, target_branch=target, enabled=reason is None, reason=reason
    )


def integration_instruction(
    binding: ConversationWorktreeBinding, option: WorktreeIntegrationOption, facts: dict
) -> str:
    context = {
        "repository_alias": binding.repository_alias,
        "worktree_path": binding.worktree_path,
        "shared_checkout_path": binding.shared_path,
        "worktree_branch": binding.branch,
        "starting_branch": binding.starting_branch,
        "starting_commit": binding.starting_commit,
        "target_branch": option.target_branch,
        "target_checked_out_in_shared_checkout": facts.get("target_checked_out", False),
    }
    instruction = (
        "RCP-authored integration requested by the human: "
        + option.label
        + ".\n"
        + json.dumps(context, ensure_ascii=False, indent=2)
        + "\nRecheck Git state before making changes. Stop and report a refusal if the worktree "
        "is dirty, a branch or checkout changed, or a merge conflicts. Never reset, stash, "
        "auto-commit dirty changes, force-push, delete branches, or remove worktrees. "
        "Task completion alone is not integration. Report the actual Git outcome.\n"
    )
    if option.id == "pull_request":
        return instruction + (
            "Use only the worktree. Push its branch to origin without force, then use gh pr create "
            "with the execution account's credentials to open a pull request for that branch. "
            "Do not modify the shared checkout. If origin is a local filesystem remote, push to "
            "that local remote only and report that GitHub pull-request creation was not exercised."
        )
    return instruction + (
        "Refuse if the shared checkout has tracked or untracked changes or the target branch "
        "does not exist locally. If the target is checked out in the shared checkout, merge "
        "the worktree branch into it there. Otherwise check the target out in the worktree, "
        "merge the worktree branch, and restore the worktree branch afterwards, including on "
        "failure (abort an in-progress merge you started before restoring). Do not switch the "
        "shared checkout away from its current branch. Do not push this local merge."
    )


def validate_conversation_worktree_execution(
    service: ProjectService,
    store: AppStore,
    project_id: str,
    request: RunRequest,
    *,
    resuming_integration: bool = False,
) -> ConversationWorktreeBinding | None:
    if not request.chat_id:
        return None
    binding = store.conversation_worktree(project_id, request.chat_id)
    if binding is None:
        if request.worktree or request.worktree_integration:
            raise ValueError("The conversation worktree binding is unavailable.")
        return None
    if request.patch_kind != "work" or request.control_episode_id:
        raise ValueError("Episodes and workers cannot use conversation worktrees.")
    validate_worktree_binding(service, request, binding, store)
    if binding.status != "ready":
        raise ValueError("The conversation worktree has not finished creation.")
    allowed_branch = (
        request.worktree_integration_target
        if (
            resuming_integration
            and request.worktree_integration in {"starting_branch", "default_branch"}
        )
        else None
    )
    if (
        request.worktree_integration in {"starting_branch", "default_branch"}
        and not request.worktree_integration_target
    ):
        raise ValueError("The integration turn has no admitted target branch.")
    worktree_command(
        store,
        host=binding.execution_host,
        operation="preflight" if request.worktree_integration else "inspect",
        binding=binding,
        allowed_branch=allowed_branch,
        target_branch=request.worktree_integration_target,
    )
    return binding


@contextmanager
def conversation_worktree_recovery_admission(
    service: ProjectService, store: AppStore, project_id: str, request: RunRequest
) -> Iterator[None]:
    """Keep removal out until recovery has durably reserved this conversation."""
    with conversation_worktree_locks(f"{store.path}:{project_id}:{request.chat_id}"):
        validate_conversation_worktree_execution(
            service, store, project_id, request, resuming_integration=True
        )
        yield


def conversation_worktree_context(
    service: ProjectService,
    store: AppStore,
    project_id: str,
    request: RunRequest,
    context: ChatContext,
    *,
    resuming_integration: bool = False,
) -> ChatContext:
    binding = validate_conversation_worktree_execution(
        service, store, project_id, request, resuming_integration=resuming_integration
    )
    if binding is None:
        return context
    return context.model_copy(
        update={
            "repositories": [
                item.model_copy(update={"path": binding.worktree_path})
                if item.alias == binding.repository_alias
                else item
                for item in context.repositories
            ]
        }
    )


def project_conversation_worktree(
    service: ProjectService,
    store: AppStore,
    project_id: str,
    request: RunRequest,
    *,
    inspect_removal: bool = False,
) -> ConversationWorktreeResponse:
    assert request.chat_id is not None
    if str(uuid.UUID(request.chat_id)) != request.chat_id:
        raise ValueError("chat_id must be a canonical UUID")
    binding = store.conversation_worktree(project_id, request.chat_id)
    response = ConversationWorktreeResponse(binding=binding)
    try:
        if binding is None:
            response.show_chooser = not store.chat_has_work_turn(project_id, request.chat_id)
            if not response.show_chooser:
                return response
            _repository(service, request)
            if _chat_busy(store, project_id, request.chat_id):
                raise ValueError("Wait for this conversation's current turn to finish.")
            # A read-only plan proves Git readiness without creating anything.
            repository = _repository(service, request)
            worktree_command(
                store,
                host=service.manifest.machine_map[repository.machine].host,
                operation="plan",
                shared_path=repository.path,
                chat_id=request.chat_id,
            )
            response.can_choose = True
            return response
        validate_worktree_binding(service, request, binding, store)
        if binding.status != "ready":
            raise ValueError("Worktree creation is unfinished. Send the first Work turn again.")
        facts = worktree_command(
            store, host=binding.execution_host, operation="inspect", binding=binding
        )
        response.ahead_count = facts["ahead_count"]
        if inspect_removal:
            remote = worktree_command(
                store, host=binding.execution_host, operation="remote_branch", binding=binding
            )
            response.remote_branch_exists = remote["remote_branch_exists"]
            response.remote_branch_evidence = remote["remote_branch_reason"]
        response.dirty_worktree = facts["dirty_worktree"]
        busy = _chat_busy(store, project_id, request.chat_id)
        common_reason = (
            "Wait for this conversation's active or paused turn to finish."
            if busy
            else "Worktree has uncommitted changes:\n" + "\n".join(facts["dirty_worktree"])
            if facts["dirty_worktree"]
            else None
        )
        response.can_remove = common_reason is None
        response.remove_reason = common_reason
        choices: list[IntegrationChoice] = ["pull_request", "starting_branch"]
        if facts["default_branch"] != binding.starting_branch:
            choices.append("default_branch")
        for choice in choices:
            option = _integration_option(binding, facts, choice)
            reason = common_reason or option.reason
            if reason is None:
                try:
                    worktree_command(
                        store,
                        host=binding.execution_host,
                        operation="preflight",
                        binding=binding,
                        target_branch=option.target_branch,
                    )
                except ValueError as exc:
                    reason = str(exc)
            response.integration_options.append(
                option.model_copy(update={"enabled": reason is None, "reason": reason})
            )
    except ValueError as exc:
        response.unavailable_reason = str(exc)
        response.remove_reason = str(exc)
        if (
            binding is not None
            and binding.status == "removing"
            and not _chat_busy(store, project_id, request.chat_id)
        ):
            response.can_remove = True
    return response


def remove_conversation_worktree(
    service: ProjectService, store: AppStore, project_id: str, chat_id: str
) -> ConversationWorktreeBinding:
    binding = store.conversation_worktree(project_id, chat_id)
    if binding is None:
        raise ValueError("This conversation has no worktree.")
    if _chat_busy(store, project_id, chat_id):
        raise ValueError("Finish the active or paused turn before removing its worktree.")
    if binding.status == "removed":
        return binding
    if binding.status == "ready":
        request = RunRequest(
            chat_id=chat_id,
            chat_scope=binding.chat_scope,
            node_id=binding.node_id,
            run_on=binding.machine,
            run_truth_scope=[binding.repository_alias],
        )
        validate_worktree_binding(service, request, binding, store)
        worktree_command(store, host=binding.execution_host, operation="preflight", binding=binding)
        binding = store.set_conversation_worktree_status(
            project_id, chat_id, expected_status="ready", status="removing"
        )
    if binding.status != "removing":
        raise ValueError("Finish worktree creation before removing it.")
    try:
        worktree_command(store, host=binding.execution_host, operation="remove", binding=binding)
    except ValueError:
        # A failed delete which left an intact worktree must not prevent Work
        # from committing dirty files. If deletion already happened, keep the
        # durable removal intent so the next explicit Remove can finish it.
        try:
            worktree_command(
                store, host=binding.execution_host, operation="inspect", binding=binding
            )
        except ValueError:
            pass
        else:
            store.set_conversation_worktree_status(
                project_id, chat_id, expected_status="removing", status="ready"
            )
        raise
    return store.set_conversation_worktree_status(
        project_id, chat_id, expected_status="removing", status="removed"
    )
