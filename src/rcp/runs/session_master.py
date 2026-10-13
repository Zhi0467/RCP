"""The durable copy of the master contract a native session was given.

A session's master is recorded byte for byte on the operation that bootstrapped it, and
restored from that record into the selected stage on every launch that points to it.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from rcp.agents.continuation_prompt import MasterRef, graph_head_delta
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.history.queries import HeadSpec
from rcp.runs.shared import _stage_or_reuse_task_input, _stage_task_contract
from rcp.storage import AppStore
from rcp.transport import RemoteRunStage

if TYPE_CHECKING:
    from rcp.background import AgentTaskExecution
    from rcp.service import ProjectService

SESSION_MASTER_ROLE = "session_master"
SESSION_MASTER_KEY_ROLE = "session_master_key"
SESSION_MASTER_VALUES_ROLE = "session_master_values"

_LABEL_DIGEST = re.compile(r"-([0-9a-f]{16})\.md\Z")


def session_master_label(prefix: str, content: str) -> str:
    """Name a master by its content, so one label always holds the same bytes."""

    return f"{prefix}-{_sha256(content)[:16]}.md"


def record_session_master(
    store: AppStore,
    operation_id: str,
    content: str,
    key: str | None = None,
    values: Mapping[str, object] | None = None,
) -> str:
    """Record the exact master bytes on the operation that sends them; return their digest.

    A key, when given, is recorded beside the bytes so a later launch in the same session
    can tell whether the master it would render now is still the one the session holds.
    The values it was rendered with let a later launch send only what has changed.
    """

    digest = _sha256(content)
    store.record_agent_task_contract(operation_id, SESSION_MASTER_ROLE, content, digest)
    if key is not None:
        store.record_agent_task_contract(operation_id, SESSION_MASTER_KEY_ROLE, key, _sha256(key))
    if values is not None:
        encoded = json.dumps(values, ensure_ascii=False, sort_keys=True)
        store.record_agent_task_contract(
            operation_id, SESSION_MASTER_VALUES_ROLE, encoded, _sha256(encoded)
        )
    return digest


def recorded_master_values(store: AppStore, operation_id: str) -> dict[str, object] | None:
    """The stable values a recorded master was rendered with, or None if none were kept."""

    encoded = store.agent_task_contract(operation_id, SESSION_MASTER_VALUES_ROLE)
    return None if encoded is None else json.loads(encoded)


def start_session_master(
    execution: AgentTaskExecution,
    *,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    label_prefix: str,
    key: str,
    content: str,
    values: Mapping[str, object] | None = None,
) -> MasterRef:
    """Record and stage the master a new native session starts from."""

    record_session_master(execution.store, execution.operation_id, content, key, values)
    path = _stage_or_reuse_task_input(
        local_stage, remote_stage, session_master_label(label_prefix, content), content
    )
    return MasterRef(path=path, bootstrap=True, values=values)


def continuation_session_master(
    execution: AgentTaskExecution,
    *,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    native_session_id: str,
    label_prefix: str,
    key: str,
    render: Callable[[], str],
    values: Mapping[str, object] | None = None,
    force_bootstrap: bool = False,
) -> MasterRef:
    """Point a continuing session at its master, or bootstrap one it does not hold.

    The session keeps its recorded master while the key matches, and the file is restored
    into the stage so the pointer resolves. A session with no recorded master, a changed
    key, or a forced bootstrap gets a freshly rendered master, recorded on this operation.
    The session id alone finds the master, so an owner that gives every turn a fresh stage
    restores it into the current one.
    ``values`` are the current stable values: recorded with a new master, and otherwise
    returned as the ones the kept master was rendered with, so the caller sends the change.
    """

    record = execution.store.agent_task(execution.operation_id)
    if record is None or not record.stage_root:
        raise ValueError("A continuation has no saved stage to hold its master contract.")
    found = execution.store.latest_session_master(record.project_id, native_session_id)
    if found is not None and found[2] == key and not force_bootstrap:
        operation_id, digest, _ = found
        content = execution.store.agent_task_contract(operation_id, SESSION_MASTER_ROLE)
        if content is None or _sha256(content) != digest:
            raise ValueError("The native session's recorded master contract is corrupt.")
        path = _stage_or_reuse_task_input(
            local_stage, remote_stage, session_master_label(label_prefix, content), content
        )
        return MasterRef(
            path=path, bootstrap=False, values=recorded_master_values(execution.store, operation_id)
        )
    content = render()
    record_session_master(execution.store, execution.operation_id, content, key, values)
    path = _stage_or_reuse_task_input(
        local_stage, remote_stage, session_master_label(label_prefix, content), content
    )
    return MasterRef(path=path, bootstrap=True, replaces=found is not None, values=values)


def stage_session_master(
    store: AppStore,
    *,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    operation_id: str,
    sha256: str,
    path: str,
) -> str:
    """Restore a recorded master into the selected stage under its own label.

    The stage itself must already exist; this writes one immutable input inside it.
    """

    content = store.agent_task_contract(operation_id, SESSION_MASTER_ROLE)
    if content is None:
        raise ValueError("The native session's recorded master contract is unavailable.")
    if _sha256(content) != sha256:
        raise ValueError("The native session's recorded master contract is corrupt.")
    return _stage_or_reuse_task_input(local_stage, remote_stage, PurePosixPath(path).name, content)


def read_legacy_session_master(
    *,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    path: str,
) -> str | None:
    """Read a pre-record master from this stage, only if its bytes match its own label."""

    label = PurePosixPath(path).name
    match = _LABEL_DIGEST.search(label)
    if match is None:
        return None
    if remote_stage is not None:
        try:
            content = remote_stage.read_input_text(label)
        except ValueError:
            return None
    else:
        assert local_stage is not None
        target = local_stage / "inputs" / label
        if target.is_symlink() or not target.is_file():
            return None
        content = target.read_text(encoding="utf-8")
    if _sha256(content)[:16] != match.group(1):
        return None
    return content


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def record_inline_prompt(
    execution: AgentTaskExecution | None,
    *,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    label: str,
    role: str,
    prompt: str,
) -> str:
    """Keep an inline continuation prompt as this attempt's contract; return its path.

    The provider receives the prompt itself, not a pointer to this file. The staged copy
    and its durable record are what Resume and Retry continue from, exactly as a chat
    turn's inline prompt already is.
    """

    contract_path, _ = _stage_task_contract(
        local_stage, remote_stage, label, prompt, execution=execution, role=role
    )
    return contract_path


def staged_graph_head(
    service: ProjectService, target: GraphTargetRef, revision: int
) -> GraphHeadRef:
    """Resolve the exact snapshot, never the live head after staging."""

    return service.history_engine.resolve(
        HeadSpec(target=target, revision=revision), default_target=target
    )


def _graph_observation_contracts(store: AppStore, operation_id: str) -> list[str]:
    return [
        record.content
        for record in store.agent_task_contracts(operation_id)
        if record.role.startswith("graph_observation:")
    ]


def prepare_graph_observation(
    execution: AgentTaskExecution | None,
    service: ProjectService,
    *,
    owner_id: str,
    target: GraphTargetRef,
    revision: int,
    native_session_id: str | None,
    previous_head: GraphHeadRef | None = None,
) -> GraphHeadRef:
    """Stage an observation candidate; only successful settlement publishes it."""

    head = staged_graph_head(service, target, revision)
    if execution is None:
        return head
    task = execution.store.agent_task(execution.operation_id)
    if task is None:
        raise ValueError("The graph-reading task record is unavailable.")
    previous = (
        execution.store.graph_seen_head_record(task.project_id, owner_id, native_session_id, target)
        if native_session_id is not None
        else None
    )
    prior_candidates = _graph_observation_contracts(execution.store, execution.operation_id)
    prior = json.loads(prior_candidates[0]) if prior_candidates else None
    if prior is not None and (
        prior["owner_id"] != owner_id
        or GraphHeadRef.model_validate(prior["head"]).target != target
        or prior["native_session_id"] not in (None, native_session_id)
    ):
        raise ValueError("Graph observation correction changed its owner or session binding.")
    content = json.dumps(
        {
            "project_id": task.project_id,
            "owner_id": owner_id,
            "native_session_id": native_session_id,
            "kind": task.kind,
            "episode_id": task.episode_id,
            "task_target": task.graph_target.model_dump(mode="json"),
            "head": head.model_dump(mode="json"),
            "previous_head": previous_head.model_dump(mode="json") if previous_head else None,
            "expected_operation_id": (
                prior["expected_operation_id"]
                if prior is not None
                else previous[1]
                if previous is not None
                else None
            ),
        }
    )
    if not prior_candidates or prior_candidates[-1] != content:
        execution.store.record_agent_task_contract(
            execution.operation_id,
            f"graph_observation:{len(prior_candidates)}",
            content,
            _sha256(content),
        )
    return head


def _own_applied_head(
    service: ProjectService,
    staged_head: GraphHeadRef,
    applied_head: GraphHeadRef | None,
    operation_id: str,
) -> GraphHeadRef:
    if applied_head is None or applied_head.target != staged_head.target:
        return staged_head
    steps = [
        step
        for step in service.history_engine.steps(applied_head)
        if step.accepted and step.after_head.revision > staged_head.revision
    ]
    if steps and all(
        step.patch.source_operation_id == operation_id or step.patch.task_id == operation_id
        for step in steps
    ):
        return applied_head
    return staged_head


def commit_prepared_graph_observation(
    execution: AgentTaskExecution | None,
    service: ProjectService,
    *,
    native_session_id: str | None,
    applied_revision: int | None = None,
) -> None:
    """Commit precisely the snapshot captured for this turn, including recovered turns."""

    if execution is None or native_session_id is None:
        return
    operation_id = execution.operation_id
    candidates = _graph_observation_contracts(execution.store, operation_id)
    content = candidates[-1] if candidates else None
    visited: set[str] = set()
    while content is None:
        if operation_id in visited:
            raise ValueError("Graph observation recovery ancestry contains a cycle.")
        visited.add(operation_id)
        task = execution.store.agent_task(operation_id)
        if task is None or task.parent_operation_id is None:
            return
        operation_id = task.parent_operation_id
        candidates = _graph_observation_contracts(execution.store, operation_id)
        content = candidates[-1] if candidates else None
    candidate = json.loads(content)
    head = GraphHeadRef.model_validate(candidate["head"])
    current = execution.store.agent_task(execution.operation_id)
    origin = execution.store.agent_task(operation_id)
    if (
        current is None
        or origin is None
        or current.project_id != candidate["project_id"]
        or current.graph_target != GraphTargetRef.model_validate(candidate["task_target"])
        or current.kind != candidate["kind"]
        or current.episode_id != candidate["episode_id"]
        or origin.project_id != current.project_id
        or origin.graph_target != current.graph_target
        or current.native_session_id not in (None, native_session_id)
        or origin.native_session_id not in (None, native_session_id)
        or candidate["native_session_id"] not in (None, native_session_id)
    ):
        raise ValueError("Graph observation recovery changed its owner, session, or target.")
    applied: GraphHeadRef | None = None
    if applied_revision is not None:
        try:
            applied = staged_graph_head(service, head.target, applied_revision)
        except (KeyError, ValueError):
            # An unprovable Apply is not absorbed: the next turn is told about it again.
            applied = None
    head = _own_applied_head(service, head, applied, execution.operation_id)
    execution.store.commit_graph_seen_head(
        project_id=candidate["project_id"],
        owner_id=candidate["owner_id"],
        native_session_id=native_session_id,
        head=head,
        operation_id=execution.operation_id,
        expected_operation_id=candidate["expected_operation_id"],
    )


def prepared_graph_observation_delta(
    execution: AgentTaskExecution | None, native_session_id: str | None
) -> dict[str, dict[str, object]]:
    """Compare this owner's captured snapshot with its last committed observation."""

    if execution is None or native_session_id is None:
        return {}
    candidates = _graph_observation_contracts(execution.store, execution.operation_id)
    if not candidates:
        return {}
    content = candidates[-1]
    candidate = json.loads(content)
    head = GraphHeadRef.model_validate(candidate["head"])
    seen = (
        GraphHeadRef.model_validate(candidate["previous_head"])
        if candidate.get("previous_head") is not None
        else execution.store.graph_seen_head(
            candidate["project_id"], candidate["owner_id"], native_session_id, head.target
        )
    )
    delta = graph_head_delta(seen, head)
    current = delta.get("current")
    return {"current": current} if isinstance(current, dict) else {}


def merge_prepared_graph_observation_delta(
    delta: Mapping[str, object] | None,
    execution: AgentTaskExecution | None,
    native_session_id: str | None,
) -> dict[str, object] | None:
    """Overlay the observation hint while retaining the owner's other current values."""

    observed = prepared_graph_observation_delta(execution, native_session_id)
    if not observed:
        return dict(delta) if delta is not None else None
    current = (delta or {}).get("current")
    return {
        **(delta or {}),
        "current": {**(current if isinstance(current, dict) else {}), **observed["current"]},
    }
