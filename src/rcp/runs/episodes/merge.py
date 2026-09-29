"""Human-dispatched owner merge, code verification, and recoverable cleanup."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from rcp.agents.context import RepositoryPointer
from rcp.agents.prompts import write_scope_section
from rcp.agents.write_scope import ProjectWriteScope
from rcp.conversation_worktrees import (
    WorktreeIntegrationOption,
    integration_instruction,
    worktree_command,
)
from rcp.core.models import (
    AuthorizedHuman,
    BranchMergeReceipt,
    EpisodeIsolation,
    EpisodeMergeAttempt,
    EpisodeWorktreeBinding,
)
from rcp.core.transition_models import GraphHeadRef
from rcp.runs.branch_merge import (
    MAX_BRANCH_MERGE_REBASE_ROUNDS,
    branch_merge_can_resolve_without_patch,
    branch_merge_id,
    branch_merge_path_dispositions,
    branch_merge_provenance,
    branch_merge_receipt_from_committed_patch,
    build_deterministic_merge_ops,
    commit_branch_merge_with_history,
    parse_branch_merge_candidate,
    prepare_branch_merge_with_history,
)
from rcp.runs.branch_merge_context import load_branch_merge_context
from rcp.runs.episodes.isolation import isolation_locks
from rcp.service import ProjectService
from rcp.storage import AppStore, EpisodeRecord


class MergeEpisodeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_branch: str | None = None
    history_mode: Literal["merge", "squash"] = "merge"
    remove_worktree: bool = True
    delete_code_branch: bool = True
    archive_graph_branch: bool = True
    keep_branch_open: bool = False

    @model_validator(mode="after")
    def coherent_cleanup(self):
        if self.history_mode == "squash" and self.keep_branch_open:
            raise ValueError("squash_keep_branch_open")
        if self.delete_code_branch and not self.remove_worktree:
            raise ValueError("delete_branch_requires_remove_worktree")
        return self


class MergeCodePreview(BaseModel):
    repo_alias: str
    source_branch: str
    target_branch: str
    commits_ahead: int
    leftover_files: list[str]
    status: Literal["clean", "conflict", "already_merged", "target_dirty", "target_missing"]
    conflict_files: list[str]


class MergeDiffPath(BaseModel):
    """One changed field on the branch, classified by the merge builder."""

    entity: Literal["node", "edge", "proposal", "ambiguity", "glossary", "global"]
    id: str
    field_path: str
    base: JsonValue = None
    branch: JsonValue = None
    main: JsonValue = None
    delivered: bool
    conflict: bool
    needs_agent: bool
    needs_proposal: bool
    residue_reason: str | None = None


class MergeGraphPreview(BaseModel):
    ops: int = 0
    residue: list[dict[str, str]] = Field(default_factory=list)
    paths: list[MergeDiffPath] = Field(default_factory=list)


class MergePreview(BaseModel):
    delivered_baseline: GraphHeadRef | None = None
    graph: MergeGraphPreview = Field(default_factory=MergeGraphPreview)
    code: MergeCodePreview | None = None
    needs_agent: bool = False


@dataclass(frozen=True)
class EpisodeCodeMerge:
    """The code half of a merge task: Integrate's local merge, for one episode worktree."""

    binding: EpisodeWorktreeBinding
    attempt: EpisodeMergeAttempt
    pointer: RepositoryPointer
    machine_writable_paths: list[str]
    target_checked_out: bool

    def prompt_section(self, scope: ProjectWriteScope) -> str:
        target = self.attempt.target_branch or self.binding.starting_branch
        option = WorktreeIntegrationOption(
            id="starting_branch",
            label=f"Merge into {target}",
            target_branch=target,
            enabled=True,
        )
        instruction = integration_instruction(
            self.binding,
            option,
            {"target_checked_out": self.target_checked_out, "resolve_conflicts": True},
        )
        conflicts = "\n".join(f"- `{path}`" for path in self.attempt.conflict_files)
        return f"""
## Code merge

RCP committed the worktree's leftovers; merge exactly commit `{self.attempt.source_commit}`.
Use a merge commit (`git merge --no-ff`), never a squash. RCP checks that `{target}` contains
that commit before it commits the graph.
{("Conflicting files from RCP's merge test:" + chr(10) + conflicts) if conflicts else "RCP's merge test found no conflicts."}

{instruction}
{write_scope_section(scope)}"""

    def landed_section(self, scope: ProjectWriteScope) -> str:
        target = self.attempt.target_branch or self.binding.starting_branch
        return f"""
## Code merge

This episode's code merge already landed in `{target}`. Leave every repository as it stands.
{write_scope_section(scope)}"""


def episode_code_merge(
    service: ProjectService, store: AppStore, owner: EpisodeRecord
) -> EpisodeCodeMerge | None:
    """The code merge this owner's merge task carries, or None for a graph-only merge."""

    state = store.episode_isolation_state(owner.project_id, owner.episode_id)
    binding = store.episode_isolation(owner.project_id, owner.episode_id)
    attempt = state.merge_attempt if state else None
    if attempt is None or not attempt.code_by_agent or binding is None or not binding.worktree:
        return None
    worktree = binding.worktree
    machine = service.manifest.machine_map[worktree.machine]
    preflight = _git(store, binding, "preflight", target_branch=attempt.target_branch)
    card = store.space_machine_for(machine.host)
    return EpisodeCodeMerge(
        binding=worktree,
        attempt=attempt,
        pointer=RepositoryPointer(
            alias=worktree.repository_alias,
            machine=worktree.machine,
            host=machine.host,
            path=worktree.worktree_path,
        ),
        machine_writable_paths=list(card.writable_paths) if card else [],
        target_checked_out=bool(preflight.get("target_checked_out")),
    )


def merge_owner(store: AppStore, member: EpisodeRecord) -> EpisodeRecord:
    owner_id = (
        member.isolation_owner_episode_id or member.graph_target.branch_id or member.episode_id
    )
    owner = store.episode(owner_id)
    if owner is None or owner.project_id != member.project_id:
        raise ValueError("episode_isolation_owner_missing")
    return owner


def _binding(store: AppStore, owner: EpisodeRecord) -> EpisodeIsolation:
    binding = store.episode_isolation(owner.project_id, owner.episode_id)
    if binding is None:
        if owner.code_worktree or owner.graph_target.kind != "branch":
            raise ValueError("episode_isolation_owner_missing")
        binding = EpisodeIsolation(
            owner_episode_id=owner.episode_id, graph_branch_id=owner.graph_target.branch_id
        )
        store.create_episode_isolation(owner.project_id, binding)
        store.set_episode_isolation_status(
            owner.project_id, owner.episode_id, expected_status="creating", status="ready"
        )
    return binding


def merge_preview(
    service: ProjectService,
    store: AppStore,
    owner: EpisodeRecord,
    *,
    authorized_by: AuthorizedHuman,
    target_branch: str | None = None,
) -> MergePreview:
    binding = store.episode_isolation(owner.project_id, owner.episode_id)
    preview = MergePreview()
    if owner.graph_target.kind == "branch":
        context = load_branch_merge_context(
            service, store, owner, operation_id="preview", authorized_by=authorized_by
        )
        ops, residue = build_deterministic_merge_ops(context)
        preview.delivered_baseline = context.review_base_head
        preview.graph = MergeGraphPreview(
            ops=len(ops),
            residue=[
                {"path": "/".join(path), "reason": reason} for path, reason in residue.items()
            ],
            paths=branch_merge_path_dispositions(context, residue),
        )
        preview.needs_agent = bool(residue)
    state = store.episode_isolation_state(owner.project_id, owner.episode_id)
    if binding and binding.worktree and not _code_discarded(state):
        worktree = binding.worktree
        try:
            if state and state.status == "removed":
                if not state.delivered_source_commit:
                    raise ValueError("code_worktree_removed")
                verified = _git(
                    store,
                    binding,
                    "verify_landing",
                    source_commit=state.delivered_source_commit,
                    target_branch=target_branch
                    or state.delivered_target_branch
                    or worktree.starting_branch,
                    squash_commit=state.squash_commit,
                )["verified"]
                if not verified:
                    raise ValueError("code_landing_unverified")
                facts = {
                    "status": "already_merged",
                    "commits_ahead": 0,
                    "leftover_files": [],
                    "conflict_files": [],
                }
            else:
                facts = worktree_command(
                    store,
                    host=worktree.execution_host,
                    operation="merge_preview",
                    binding=worktree,
                    target_branch=target_branch or worktree.starting_branch,
                )
        except ValueError as exc:
            code = getattr(exc, "code", str(exc).split(":", 1)[0])
            if code not in {"target_missing", "target_dirty"}:
                raise
            facts = {"status": code, "commits_ahead": 0, "leftover_files": [], "conflict_files": []}
        preview.code = MergeCodePreview(
            repo_alias=worktree.repository_alias,
            source_branch=worktree.branch,
            target_branch=target_branch or worktree.starting_branch,
            **{
                key: facts[key]
                for key in ("commits_ahead", "leftover_files", "status", "conflict_files")
            },
        )
        preview.needs_agent |= facts["status"] == "conflict"
    return preview


def _code_discarded(state) -> bool:
    """A removed worktree whose code never landed was discarded on purpose."""
    return state is not None and state.status == "removed" and not state.delivered_source_commit


def _save(
    store: AppStore, owner: EpisodeRecord, attempt: EpisodeMergeAttempt, **changes
) -> EpisodeMergeAttempt:
    attempt = attempt.model_copy(update=changes)
    store.update_episode_merge_attempt(
        owner.project_id, owner.episode_id, expected_attempt_id=attempt.attempt_id, attempt=attempt
    )
    return attempt


def _git(store, binding, operation, **values):
    return worktree_command(
        store,
        host=binding.worktree.execution_host,
        binding=binding.worktree,
        operation=operation,
        **values,
    )


def verify_episode_merge_code(store, owner):
    state = store.episode_isolation_state(owner.project_id, owner.episode_id)
    binding = store.episode_isolation(owner.project_id, owner.episode_id)
    if binding is None or binding.worktree is None:
        return
    if state is None or state.merge_attempt is None or not state.merge_reservation:
        raise ValueError("episode_merge_reserved")
    attempt = state.merge_attempt
    if attempt.source_commit is None:
        return
    if not _git(
        store,
        binding,
        "verify_landing",
        source_commit=attempt.source_commit,
        target_branch=attempt.target_branch,
        squash_commit=attempt.squash_commit,
        target_commit=attempt.target_commit,
    )["verified"]:
        raise ValueError("code_landing_unverified")


def _commit_graph(service, store, owner, attempt):
    from rcp.history import RevisionConflict

    for retry in range(MAX_BRANCH_MERGE_REBASE_ROUNDS):
        try:
            return _commit_graph_once(service, store, owner, attempt)
        except RevisionConflict:
            if retry == MAX_BRANCH_MERGE_REBASE_ROUNDS - 1:
                raise


def _graph_receipt(service, store, owner, attempt):
    """This attempt's canonical graph receipt, if its graph already merged."""
    binding = store.episode_isolation(owner.project_id, owner.episode_id)
    if binding is None or binding.graph_branch_id is None:
        return None
    context = load_branch_merge_context(
        service, store, owner, operation_id=attempt.attempt_id, authorized_by=attempt.authorized_by
    )
    branch = service.history.branch(
        owner.episode_id, expected_episode_id=owner.episode_id, expected_project_id=owner.project_id
    )
    existing = branch.reconcile_merge_receipt(branch_merge_id(context.metadata))
    return (
        existing if existing and existing.provenance.merge_task_id == attempt.attempt_id else None
    )


def _commit_graph_once(service, store, owner, attempt):
    binding = store.episode_isolation(owner.project_id, owner.episode_id)
    if binding is None or binding.graph_branch_id is None:
        return
    context = load_branch_merge_context(
        service, store, owner, operation_id=attempt.attempt_id, authorized_by=attempt.authorized_by
    )
    if attempt.graph_head is not None and context.metadata.head != attempt.graph_head:
        raise ValueError("merge_graph_head_changed")
    branch = service.history.branch(
        owner.episode_id, expected_episode_id=owner.episode_id, expected_project_id=owner.project_id
    )
    existing = branch.reconcile_merge_receipt(branch_merge_id(context.metadata))
    if existing is not None:
        return existing if existing.provenance.merge_task_id == attempt.attempt_id else None
    ops, residue = build_deterministic_merge_ops(context)
    if residue:
        raise ValueError("graph_residue_needs_merge_task")
    if branch_merge_can_resolve_without_patch(context):
        verify_episode_merge_code(store, owner)
        receipt = BranchMergeReceipt(
            outcome="no_change",
            provenance=branch_merge_provenance(context),
            result_main_head=context.main_head,
            authorized_by=attempt.authorized_by,
        )
    else:
        candidate = parse_branch_merge_candidate(
            json.dumps({"summary": "Merge episode graph changes.", "ops": []}),
            context,
            deterministic_ops=ops,
        )
        prepare_branch_merge_with_history(
            service.history, candidate, expected_main_head=context.main_head, context=context
        )
        verify_episode_merge_code(store, owner)
        committed = commit_branch_merge_with_history(
            service.history, candidate, expected_main_head=context.main_head
        )
        receipt = branch_merge_receipt_from_committed_patch(committed.patch)
    branch.write_merge_receipt(receipt)
    store.record_agent_task_receipt(
        attempt.attempt_id, "branch_merge_outcome", receipt.model_dump(mode="json")
    )
    return receipt


def _cleanup(service, store, owner, binding, attempt):
    attempt = _save(store, owner, attempt, phase="cleanup", error=None)
    code_side = binding.worktree is not None and attempt.source_commit is not None
    errors = []
    for step, enabled in (
        ("remove_worktree", attempt.remove_worktree and code_side),
        ("delete_code_branch", attempt.delete_code_branch and code_side),
        (
            "archive_graph_branch",
            attempt.archive_graph_branch and binding.graph_branch_id is not None,
        ),
    ):
        if attempt.keep_branch_open or not enabled or step in attempt.cleanup_completed:
            continue
        try:
            if step == "remove_worktree":
                store.require_episode_binding_quiescent(owner.project_id, owner.episode_id)
                _git(
                    store,
                    binding,
                    "remove_episode_worktree",
                    discard=attempt.confirm_discard,
                    source_commit=attempt.source_commit,
                )
            elif step == "delete_code_branch":
                if "remove_worktree" not in attempt.cleanup_completed:
                    raise ValueError("delete_branch_requires_remove_worktree")
                _git(
                    store,
                    binding,
                    "delete_branch",
                    source_commit=attempt.source_commit,
                    target_branch=attempt.target_branch,
                    squash_commit=attempt.squash_commit,
                    target_commit=attempt.target_commit,
                )
            completed = attempt.model_copy(
                update={"cleanup_completed": [*attempt.cleanup_completed, step]}
            )
            # One write, so a crash cannot record the step without its effect.
            store.update_episode_merge_attempt(
                owner.project_id,
                owner.episode_id,
                expected_attempt_id=attempt.attempt_id,
                attempt=completed,
                **({"graph_archived": True} if step == "archive_graph_branch" else {}),
            )
            attempt = completed
        except (ValueError, OSError) as exc:
            errors.append(str(exc))
    if errors:
        _save(store, owner, attempt, error="; ".join(errors))
        raise ValueError("episode_cleanup_failed")
    attempt = _save(store, owner, attempt, phase="done")
    store.finish_episode_merge(
        owner.project_id, owner.episode_id, expected_attempt_id=attempt.attempt_id, attempt=attempt
    )
    return attempt


def _resume(service, store, owner, binding, attempt):
    if attempt.phase == "agent_merging":
        # Only the merge task's completion, or its reconciliation, moves past this phase;
        # nothing here may land, commit, or clean up undelivered work.
        raise ValueError("episode_merge_reserved")
    if attempt.phase == "landing":
        # No source commit means this attempt carries no code side.
        if binding.worktree and attempt.source_commit:
            verified = _git(
                store,
                binding,
                "verify_landing",
                source_commit=attempt.source_commit,
                target_branch=attempt.target_branch,
                squash_commit=attempt.squash_commit,
                target_commit=attempt.target_commit,
            )["verified"]
            if not verified:
                _git(
                    store,
                    binding,
                    "land",
                    **{
                        name: getattr(attempt, name)
                        for name in (
                            "source_commit",
                            "target_commit",
                            "target_branch",
                            "tree",
                            "history_mode",
                            "landed_commit",
                            "squash_commit",
                            "commit_timestamp",
                            "expected_shared_branch",
                        )
                    },
                )
                if not _git(
                    store,
                    binding,
                    "verify_landing",
                    source_commit=attempt.source_commit,
                    target_branch=attempt.target_branch,
                    squash_commit=attempt.squash_commit,
                    target_commit=attempt.target_commit,
                )["verified"]:
                    raise ValueError("code_landing_unverified")
        attempt = _save(store, owner, attempt, phase="verified")
        # A graph-only attempt keeps the code delivery an earlier attempt recorded.
        if attempt.source_commit:
            store.update_episode_merge_attempt(
                owner.project_id,
                owner.episode_id,
                expected_attempt_id=attempt.attempt_id,
                attempt=attempt,
                delivered_source_commit=attempt.source_commit,
                delivered_target_branch=attempt.target_branch,
                squash_commit=attempt.squash_commit,
            )
    if attempt.phase == "verified" and attempt.graph_task_id:
        return attempt
    if attempt.phase == "verified":
        _commit_graph(service, store, owner, attempt)
        attempt = _save(store, owner, attempt, phase="graph_committed")
    if attempt.phase == "graph_committed":
        task = store.agent_task(attempt.attempt_id)
        if task is not None and task.status != "succeeded":
            receipt = _commit_graph(service, store, owner, attempt)
            if receipt is not None:
                store.reconcile_agentless_merge_task(attempt.attempt_id, receipt)
    return _cleanup(service, store, owner, binding, attempt)


def merge_episode(
    service: ProjectService,
    store: AppStore,
    owner: EpisodeRecord,
    body: MergeEpisodeBody,
    *,
    authorized_by: AuthorizedHuman,
    dispatch_graph,
):
    """Reserve before the first Git write; retained phases reconcile uncertain results."""
    with isolation_locks(f"{store.path}:{owner.project_id}:{owner.episode_id}"):
        binding = _binding(store, owner)
        state = store.episode_isolation_state(owner.project_id, owner.episode_id)
        previous = state.merge_attempt
        if state.merge_reservation and previous:
            if previous.graph_task_id:
                task = store.agent_task(previous.graph_task_id)
                if task and task.status in {"queued", "running", "pausing"}:
                    raise ValueError("episode_merge_reserved")
            if previous.phase != "pre_merge":
                return _resume(service, store, owner, binding, previous)
            store.finish_episode_merge(
                owner.project_id,
                owner.episode_id,
                expected_attempt_id=previous.attempt_id,
                attempt=previous,
            )
        code_side = binding.worktree is not None and state.status != "removed"
        attempt = EpisodeMergeAttempt(
            attempt_id=str(uuid.uuid4()), authorized_by=authorized_by, **body.model_dump()
        )
        store.reserve_episode_merge(owner.project_id, owner.episode_id, attempt)
        try:
            context = None
            residue = {}
            if binding.graph_branch_id:
                context = load_branch_merge_context(
                    service,
                    store,
                    owner,
                    operation_id=attempt.attempt_id,
                    authorized_by=authorized_by,
                )
                _, residue = build_deterministic_merge_ops(context)
            if (
                not code_side
                and context is not None
                and (
                    context.metadata.head.revision <= context.metadata.base_head.revision
                    or (
                        context.previous_merge_receipt is not None
                        and context.previous_merge_receipt.provenance.branch_head
                        == context.metadata.head
                    )
                )
            ):
                raise ValueError("no_undelivered_work")
            facts = {}
            if code_side:
                target = body.target_branch or binding.worktree.starting_branch
                _git(store, binding, "commit_leftovers", target_branch=target)
                preview = _git(store, binding, "merge_preview", target_branch=target)
                if preview["status"] in {"target_missing", "target_dirty"}:
                    raise ValueError(preview["status"])
                already_merged = preview["status"] == "already_merged"
                facts = {key: preview[key] for key in ("source_commit", "target_commit", "tree")}
                facts["target_branch"] = target
                code_by_agent = not already_merged and (
                    bool(residue) or preview["status"] == "conflict"
                )
                if code_by_agent:
                    # When a merge task runs, its agent lands the code the way Integrate does.
                    if body.history_mode == "squash":
                        raise ValueError("squash_needs_agentless_merge")
                    facts.update(
                        code_by_agent=True,
                        conflict_files=list(preview.get("conflict_files") or []),
                        merge_tree_output=preview.get("merge_tree_output"),
                    )
                elif not already_merged:
                    facts.update(
                        _git(
                            store,
                            binding,
                            "prepare_landing",
                            history_mode=body.history_mode,
                            **facts,
                        )
                    )
            graph_delivered = (
                context is not None
                and context.previous_merge_receipt is not None
                and context.previous_merge_receipt.provenance.branch_head == context.metadata.head
            )
            if (
                context is not None
                and not residue
                and not graph_delivered
                and not facts.get("code_by_agent")
            ):
                dispatch_graph(attempt.attempt_id, launch=False)
                store.mark_agent_task_running(attempt.attempt_id)
            needs_task = bool(residue) or bool(facts.get("code_by_agent"))
            attempt = _save(
                store,
                owner,
                attempt,
                phase="agent_merging" if facts.get("code_by_agent") else "landing",
                graph_head=context.metadata.head if context else None,
                graph_task_id=attempt.attempt_id if needs_task else None,
                **facts,
            )
            if needs_task:
                if attempt.phase == "landing":
                    _resume(service, store, owner, binding, attempt)
                return dispatch_graph(attempt.attempt_id)
            return _resume(service, store, owner, binding, attempt)
        except Exception as exc:
            current = store.episode_isolation_state(
                owner.project_id, owner.episode_id
            ).merge_attempt
            _save(store, owner, current, error=str(exc))
            task = store.agent_task(current.attempt_id)
            if (
                current.phase in {"pre_merge", "verified"}
                and task
                and task.status in {"queued", "running", "pausing"}
            ):
                store.fail_agent_task(current.attempt_id, str(exc))
            if current.phase == "pre_merge":
                store.finish_episode_merge(
                    owner.project_id,
                    owner.episode_id,
                    expected_attempt_id=current.attempt_id,
                    attempt=current,
                )
            raise


def complete_graph_merge(service, store, owner):
    state = store.episode_isolation_state(owner.project_id, owner.episode_id)
    if state is None or state.merge_attempt is None or not state.merge_reservation:
        return
    attempt = state.merge_attempt
    if attempt.graph_task_id is not None:
        if attempt.code_by_agent:
            store.update_episode_merge_attempt(
                owner.project_id,
                owner.episode_id,
                expected_attempt_id=attempt.attempt_id,
                attempt=attempt,
                delivered_source_commit=attempt.source_commit,
                delivered_target_branch=attempt.target_branch,
            )
        attempt = _save(store, owner, attempt, phase="graph_committed")
        try:
            _cleanup(service, store, owner, _binding(store, owner), attempt)
        except (ValueError, OSError) as exc:
            current = store.episode_isolation_state(
                owner.project_id, owner.episode_id
            ).merge_attempt
            _save(store, owner, current, error=str(exc))


def reconcile_episode_merge(service, store, owner):
    with isolation_locks(f"{store.path}:{owner.project_id}:{owner.episode_id}"):
        state = store.episode_isolation_state(owner.project_id, owner.episode_id)
        if state is None or not state.merge_reservation or state.merge_attempt is None:
            return
        attempt = state.merge_attempt
        if attempt.phase == "pre_merge":
            task = store.agent_task(attempt.attempt_id)
            if task and task.status in {"queued", "running", "pausing"}:
                store.fail_agent_task(
                    attempt.attempt_id, "episode_merge_pre_merge_reclaimed", status="interrupted"
                )
            store.finish_episode_merge(
                owner.project_id,
                owner.episode_id,
                expected_attempt_id=attempt.attempt_id,
                attempt=attempt,
            )
            return
        if attempt.phase == "done":
            store.finish_episode_merge(
                owner.project_id,
                owner.episode_id,
                expected_attempt_id=attempt.attempt_id,
                attempt=attempt,
            )
            return attempt
        if attempt.graph_task_id:
            task = store.agent_task(attempt.graph_task_id)
            if task and task.status in {"queued", "running", "pausing"}:
                return
            # A task can fail after writing its graph receipt; that graph did merge.
            if (task and task.status == "succeeded") or _graph_receipt(
                service, store, owner, attempt
            ) is not None:
                complete_graph_merge(service, store, owner)
                return store.episode_isolation_state(
                    owner.project_id, owner.episode_id
                ).merge_attempt
            store.finish_episode_merge(
                owner.project_id,
                owner.episode_id,
                expected_attempt_id=attempt.attempt_id,
                attempt=attempt,
            )
            return
        task = store.agent_task(attempt.attempt_id)
        if (
            attempt.phase == "verified"
            and task
            and task.status in {"failed", "interrupted", "paused"}
            # A crash can fall after the graph receipt and before the phase that records it.
            and _graph_receipt(service, store, owner, attempt) is None
        ):
            store.finish_episode_merge(
                owner.project_id,
                owner.episode_id,
                expected_attempt_id=attempt.attempt_id,
                attempt=attempt,
            )
            return
        return _resume(service, store, owner, _binding(store, owner), attempt)


class CleanupEpisodeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    remove_worktree: bool = False
    delete_code_branch: bool = False
    archive_graph_branch: bool = False
    confirm_discard: bool = False


def cleanup_episode(service, store, owner, body: CleanupEpisodeBody, *, authorized_by):
    with isolation_locks(f"{store.path}:{owner.project_id}:{owner.episode_id}"):
        binding = _binding(store, owner)
        state = store.episode_isolation_state(owner.project_id, owner.episode_id)
        if state.merge_reservation:
            if state.merge_attempt.phase != "cleanup":
                raise ValueError("episode_merge_reserved")
            attempt = _save(
                store,
                owner,
                state.merge_attempt,
                remove_worktree=body.remove_worktree
                or "remove_worktree" in state.merge_attempt.cleanup_completed,
                delete_code_branch=body.delete_code_branch,
                archive_graph_branch=body.archive_graph_branch,
            )
            return _cleanup(service, store, owner, binding, attempt)
        graph_delivered = binding.graph_branch_id is None
        if binding.graph_branch_id:
            context = load_branch_merge_context(
                service, store, owner, operation_id="cleanup", authorized_by=authorized_by
            )
            graph_delivered = (
                context.previous_merge_receipt is not None
                and context.previous_merge_receipt.provenance.branch_head == context.metadata.head
            )
        code_delivered = binding.worktree is None
        if binding.worktree and state.status == "removed" and state.delivered_source_commit:
            code_delivered = _git(
                store,
                binding,
                "verify_landing",
                source_commit=state.delivered_source_commit,
                target_branch=state.delivered_target_branch,
                squash_commit=state.squash_commit,
            )["verified"]
        facts = {}
        if (
            binding.worktree
            and state.status != "removed"
            and (body.remove_worktree or body.delete_code_branch)
        ):
            target = state.delivered_target_branch or binding.worktree.starting_branch
            facts = _git(store, binding, "merge_preview", target_branch=target)
            code_delivered = not facts["leftover_files"] and (
                facts["status"] == "already_merged"
                or (
                    state.delivered_source_commit == facts["source_commit"]
                    and bool(state.squash_commit)
                )
            )
        if (
            (body.remove_worktree and not code_delivered)
            or (body.archive_graph_branch and not graph_delivered)
        ) and not body.confirm_discard:
            raise ValueError("unmerged_cleanup_confirmation_required")
        if body.delete_code_branch and (
            (not body.remove_worktree and state.status != "removed") or not code_delivered
        ):
            raise ValueError("delete_branch_requires_delivery_and_removal")
        attempt = EpisodeMergeAttempt(
            attempt_id=str(uuid.uuid4()),
            authorized_by=authorized_by,
            phase="cleanup",
            remove_worktree=body.remove_worktree
            or (body.delete_code_branch and state.status == "removed"),
            delete_code_branch=body.delete_code_branch,
            archive_graph_branch=body.archive_graph_branch,
            source_commit=facts.get("source_commit") or state.delivered_source_commit,
            target_branch=state.delivered_target_branch
            or (binding.worktree.starting_branch if binding.worktree else None),
            squash_commit=state.squash_commit,
            confirm_discard=body.confirm_discard,
            cleanup_completed=["remove_worktree"] if state.status == "removed" else [],
        )
        store.reserve_episode_merge(owner.project_id, owner.episode_id, attempt)
        return _cleanup(service, store, owner, binding, attempt)
