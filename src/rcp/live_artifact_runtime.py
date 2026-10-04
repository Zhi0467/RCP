"""Version-bound live reads and server-owned final captures.

Only discovery/publication supplies declarations. Viewer requests can select an
existing version, never a path, host, graph target, or new need.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

from rcp.artifacts import read_local_regular_file
from rcp.core.project_types import project_type_of
from rcp.limits import (
    LIVE_ARTIFACT_LOG_TAIL_LINES,
    LIVE_ARTIFACT_MAX_BYTES,
    LIVE_ARTIFACT_MAX_CAPTURE_ATTEMPTS,
    LIVE_ARTIFACT_MAX_FILES,
    LIVE_ARTIFACT_MAX_RETRY_SECONDS,
    LIVE_ARTIFACT_MAX_ROWS,
    LIVE_ARTIFACT_MAX_SCANNED_ENTRIES,
    LIVE_ARTIFACT_MAX_TOTAL_BYTES,
    LIVE_ARTIFACT_REFRESH_SECONDS,
    LIVE_ARTIFACT_RETRY_SECONDS,
    LIVE_ARTIFACT_SSH_REFRESH_SECONDS,
)
from rcp.live_artifacts import (
    EpisodeSnapshot,
    FileSnapshot,
    FilesSnapshot,
    JobSnapshot,
    LiveDataMessage,
    LiveEvidence,
    NodeSnapshot,
    ResolvedLiveNeed,
    ResolvedLiveVersion,
    parse_live_tag,
)
from rcp.regular_file_reader import read_local_regular_files
from rcp.transport import RemoteRunStage, StateUnavailable

logger = logging.getLogger(__name__)
_LIVE_DISABLED_REASON = "Live data is disabled for this artifact."


def _version(store, artifact_id, version_id):
    artifact = store.artifact(artifact_id)
    if artifact is None or store.project(artifact.project_id) is None:
        raise KeyError(artifact_id)
    version = next(
        (v for v in store.artifact_versions(artifact_id) if v.version_id == version_id), None
    )
    if version is None:
        raise KeyError(version_id)
    return artifact, version


def _task_episode_id(task):
    edit = task.request.get("artifact_edit")
    return edit.get("episode_id") if isinstance(edit, dict) else task.episode_id


def _owner(store, artifact):
    task = store.agent_task(artifact.origin_operation_id) if artifact.origin_operation_id else None
    if task is None or task.project_id != artifact.project_id:
        raise ValueError("The artifact's original task is unavailable.")
    if artifact.episode_id != _task_episode_id(task):
        raise ValueError("The artifact no longer belongs to its original task episode.")
    episode = store.episode(artifact.episode_id) if artifact.episode_id else None
    if artifact.episode_id and (
        episode is None
        or episode.project_id != artifact.project_id
        or episode.graph_target != task.graph_target
    ):
        raise ValueError("The artifact's episode graph target is unavailable.")
    return task, episode


def _graph_history(service, task):
    from rcp.history.branches import BranchHistoryManager

    history = service.history
    if isinstance(history, BranchHistoryManager):
        # Discovery runs inside its turn's already-scoped service. Reopen from
        # the canonical owner so branch metadata and project identity are
        # checked again, without asking a branch to create another branch.
        if task.graph_target.kind == "main":
            return history.parent
        return history.parent.branch(
            task.graph_target.branch_id,
            expected_episode_id=task.graph_target.branch_id,
            expected_project_id=task.project_id,
            initialize=False,
        )
    return service.for_graph_target(
        task.graph_target,
        expected_episode_id=task.graph_target.branch_id,
        initialize=False,
    ).history


def _readable_roots(store, service, task, host):
    roots = [
        repository.path
        for repository in service.manifest.repositories
        if (service.manifest.machine_map[repository.machine].host or None) == (host or None)
    ]
    binding = None
    episode_id = _task_episode_id(task)
    if episode_id:
        episode = store.episode(episode_id)
        owner_id = episode.isolation_owner_episode_id or episode.episode_id
        owner = store.episode(owner_id)
        if owner is None or owner.project_id != task.project_id:
            raise ValueError("The artifact's worktree owner is unavailable.")
        isolation = store.episode_isolation(task.project_id, owner_id)
        state = store.episode_isolation_state(task.project_id, owner_id)
        if isolation is not None and state is not None and state.status == "ready":
            binding = isolation.worktree
    else:
        chat_id = task.request.get("chat_id")
        if isinstance(chat_id, str):
            candidate = store.conversation_worktree(task.project_id, chat_id)
            if candidate is not None and candidate.status == "ready":
                binding = candidate
    if binding is not None:
        repository = service.manifest.repository_map.get(binding.repository_alias)
        if (
            repository is None
            or repository.machine != binding.machine
            or (binding.execution_host or None) != (host or None)
            or service.manifest.machine_map[repository.machine].host != binding.execution_host
        ):
            raise ValueError("The artifact's worktree repository or host changed.")
        # This is an immutable owner binding, not an arbitrary path pointer or
        # temporary write grant. A moved registration revokes its live reads.
        if PurePosixPath(repository.path) != PurePosixPath(binding.shared_path):
            raise ValueError("The artifact's registered worktree repository moved.")
        roots.append(binding.worktree_path)
    return roots


def _file_root(store, service, task, host, path):
    candidate = PurePosixPath(path)
    roots = [
        root
        for root in _readable_roots(store, service, task, host)
        if candidate.is_relative_to(PurePosixPath(root))
    ]
    if not roots:
        raise ValueError(
            "Live file is outside this project's readable roots on its execution host."
        )
    return max(roots, key=len)


def resolve_artifact_live_version(
    store, service, artifact_id: str, version_id: str
) -> ResolvedLiveVersion | None:
    """Resolve once after discovery or publication; edit publishers call this too."""
    with store.artifact_lock(artifact_id):
        artifact, version = _version(store, artifact_id, version_id)
        if version.live is not None and artifact.live_data_allowed:
            return version.live
        if artifact.media_type != "text/html":
            return None
        resolved = ResolvedLiveVersion()
        try:
            tag = parse_live_tag(
                store.read_artifact_bytes(artifact_id, version_id).decode("utf-8"),
                allow_episode=bool(artifact.episode_id),
            )
            if tag is None:
                return None
            if not artifact.live_data_allowed:
                raise ValueError(_LIVE_DISABLED_REASON)
            task, episode = _owner(store, artifact)
            graph_history = _graph_history(service, task)
            graph = None
            resolved.graph_branch_id = task.graph_target.branch_id
            resolved.execution_host = task.stage_host or None
            for need in tag.needs:
                binding = ResolvedLiveNeed(need=need)
                if need.kind == "job":
                    from rcp.runs.tasks.compute_commands import compute_recovery_lineage

                    receipts = store.compute_command_receipts(
                        compute_recovery_lineage(store, task.operation_id), "launch", need.key
                    )
                    ids = {
                        item.get("response", {}).get("result", {}).get("job_id")
                        for item in receipts
                        if item.get("response")
                    }
                    ids.discard(None)
                    if len(ids) != 1:
                        raise ValueError(f"Launch key {need.key!r} has no unique recorded job.")
                    binding.job_id = ids.pop()
                    job = store.compute_job(binding.job_id)
                    if (
                        job is None
                        or job.project_id != artifact.project_id
                        or job.origin_operation_id
                        not in compute_recovery_lineage(store, task.operation_id)
                    ):
                        raise ValueError("Launch key names a job outside this artifact's lineage.")
                elif need.kind == "node":
                    graph = graph or graph_history.state()
                    if need.id not in graph.nodes:
                        raise ValueError(f"Node {need.id!r} is not in the artifact's graph.")
                    binding.graph_branch_id = task.graph_target.branch_id
                elif need.kind == "episode":
                    if episode is None:
                        raise ValueError("An episode need requires the artifact's own episode.")
                    binding.episode_id = episode.episode_id
                else:
                    binding.host = task.stage_host or None
                    binding.root = _file_root(
                        store,
                        service,
                        task,
                        binding.host,
                        need.dir if need.kind == "files" else need.path,
                    )
                resolved.needs.append(binding)
        except (ValueError, KeyError, OSError, StateUnavailable) as exc:
            resolved.needs = []
            resolved.invalid_reason = str(exc)
        store.set_artifact_version_live(artifact_id, version_id, resolved)
        return resolved


def _read(host, path, *, tail):
    # One extra byte detects a clipped first line. The data delivered to the page
    # remains within MAX_BYTES; directory components and the file refuse symlinks.
    limit = LIVE_ARTIFACT_MAX_BYTES + (1 if tail else 0)
    data = (
        RemoteRunStage(host).read_live_file(path, max_bytes=limit, tail=tail)
        if host
        else read_local_regular_file(Path(path).parent, Path(path).name, max_bytes=limit, tail=tail)
    )
    truncated = len(data) > LIVE_ARTIFACT_MAX_BYTES
    if truncated:
        boundary = data[:1] == b"\n"
        data = data[1:]
        if not boundary:
            data = data.split(b"\n", 1)[1] if b"\n" in data else b""
    return data.decode("utf-8"), truncated


def _file_snapshot(binding, *, final=False):
    need = binding.need
    text, truncated = _read(binding.host, need.path, tail=need.read == "tail")
    return _decode_file(need, need.path, text, truncated, final=final)


def _decode_file(need, path, text, truncated, *, final=False):
    byte_truncated = truncated
    if text and not text.endswith("\n"):
        discard_last = need.format == "csv" and not final
        if need.format == "jsonl":
            try:
                json.loads(text.rpartition("\n")[2])
            except json.JSONDecodeError:
                discard_last = True
        if discard_last:
            text = text.rpartition("\n")[0]
            truncated = True
    if need.format == "jsonl":
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    elif need.format == "csv":
        # Arrays preserve the actual tail without inventing a header that may
        # have fallen outside the bounded read.
        if byte_truncated:
            raise ValueError(
                "CSV exceeds the byte cap; a clipped tail cannot establish CSV record boundaries."
            )
        rows = list(csv.reader(io.StringIO(text), strict=True))
    else:
        rows = text.splitlines()
    truncated = truncated or len(rows) > LIVE_ARTIFACT_MAX_ROWS
    rows = rows[-LIVE_ARTIFACT_MAX_ROWS:] if need.read == "tail" else rows[:LIVE_ARTIFACT_MAX_ROWS]
    return FileSnapshot(path=path, rows=rows, truncated=truncated)


def _files_snapshot(binding, *, final=False):
    need = binding.need
    limits = dict(
        max_files=LIVE_ARTIFACT_MAX_FILES,
        max_total_bytes=LIVE_ARTIFACT_MAX_TOTAL_BYTES,
        max_bytes=LIVE_ARTIFACT_MAX_BYTES,
        max_entries=LIVE_ARTIFACT_MAX_SCANNED_ENTRIES,
        tail=need.read == "tail",
    )
    result = (
        RemoteRunStage(binding.host).read_live_files(need.dir, need.pattern, **limits)
        if binding.host
        else read_local_regular_files(Path(need.dir), need.pattern, **limits)
    )
    files = []
    for item in result["files"]:
        try:
            if item.get("error"):
                raise ValueError(item["error"])
            snapshot = _decode_file(
                need,
                item["path"],
                base64.b64decode(item["data"]).decode("utf-8"),
                item["truncated"],
                final=final,
            )
        except (ValueError, UnicodeError, csv.Error) as exc:
            snapshot = FileSnapshot(path=item["path"], error=str(exc), truncated=item["truncated"])
        files.append(snapshot)
    return FilesSnapshot(
        dir=need.dir,
        files=files,
        truncated=result["truncated"] or any(item.truncated for item in files),
        error="Some matched files could not be read."
        if any(item.error for item in files)
        else None,
    )


def _validate_read(store, service, artifact, version):
    task, episode = _owner(store, artifact)
    live = version.live
    if task.history_only:
        raise ValueError("Historical artifacts cannot resume live source reads.")
    if live.graph_branch_id != task.graph_target.branch_id or live.execution_host != (
        task.stage_host or None
    ):
        raise ValueError("The artifact's recorded graph target or execution host changed.")
    graph_history = _graph_history(service, task)
    for binding in live.needs:
        need = binding.need
        if need.kind in {"file", "files"} and (
            binding.host != live.execution_host
            or binding.root
            != _file_root(
                store,
                service,
                task,
                binding.host,
                need.dir if need.kind == "files" else need.path,
            )
        ):
            raise ValueError("The live file's readable root changed.")
        if need.kind == "node" and binding.graph_branch_id != live.graph_branch_id:
            raise ValueError("The live node's graph target changed.")
        if need.kind == "episode" and (episode is None or binding.episode_id != episode.episode_id):
            raise ValueError("The live episode binding changed.")
        if need.kind == "job":
            from rcp.runs.tasks.compute_commands import compute_recovery_lineage

            job = store.compute_job(binding.job_id)
            if (
                job is None
                or job.project_id != artifact.project_id
                or job.origin_operation_id not in compute_recovery_lineage(store, task.operation_id)
            ):
                raise ValueError("The live job no longer belongs to this artifact's lineage.")
    return graph_history


def _snapshot(store, service, artifact, version, *, final=False):
    graph_history = _validate_read(store, service, artifact, version)
    snapshots = []
    graph = None
    for binding in version.live.needs:
        need = binding.need
        try:
            if need.kind == "file":
                snapshot = _file_snapshot(binding, final=final)
            elif need.kind == "files":
                snapshot = _files_snapshot(binding, final=final)
            elif need.kind == "job":
                job = store.compute_job(binding.job_id)
                log, _ = _read(job.execution_host, job.log_path, tail=True)
                snapshot = JobSnapshot(
                    key=need.key,
                    state=job.status,
                    exit_code=job.exit_status,
                    started_at=job.started_at,
                    ended_at=job.ended_at,
                    log_tail="\n".join(log.splitlines()[-LIVE_ARTIFACT_LOG_TAIL_LINES:]),
                )
            elif need.kind == "episode":
                episode = store.episode(binding.episode_id)
                meter = store.episode_budget_meter(binding.episode_id)
                snapshot = EpisodeSnapshot(
                    turn=episode.invocations_used,
                    turn_limit=episode.invocation_ceiling,
                    budget_used=meter.invocations_used,
                    budget_limit=meter.invocation_ceiling,
                    state=episode.status,
                )
            else:
                graph = graph or graph_history.state()
                node = graph.nodes[need.id]
                project_type = project_type_of(graph)
                evidence = []
                for edge in graph.edges.values():
                    other_id = (
                        edge.source
                        if edge.target == need.id
                        else edge.target
                        if edge.source == need.id
                        else None
                    )
                    other = graph.nodes.get(other_id)
                    if other is not None and project_type.is_outcome(other.type):
                        evidence.append(
                            LiveEvidence(id=other.id, title=other.title, stance=edge.relation)
                        )
                snapshot = NodeSnapshot(
                    id=need.id,
                    title=node.title,
                    type=node.type,
                    status=getattr(node, "status", None),
                    evidence=evidence[:LIVE_ARTIFACT_MAX_ROWS],
                )
        except Exception as exc:
            # Keep the failing source visible and never turn an outage into an
            # empty successful final capture.
            cls = {
                "file": FileSnapshot,
                "files": FilesSnapshot,
                "job": JobSnapshot,
                "node": NodeSnapshot,
                "episode": EpisodeSnapshot,
            }[need.kind]
            identity = {
                key: getattr(need, key)
                for key in ("path", "dir", "key", "id")
                if hasattr(need, key)
            }
            snapshot = cls(**identity, error=str(exc))
        snapshots.append(snapshot)
    return LiveDataMessage(
        snapshots=snapshots,
        complete=all(item.error is None for item in snapshots),
        refresh_seconds=LIVE_ARTIFACT_SSH_REFRESH_SECONDS
        if any(binding.host for binding in version.live.needs)
        else LIVE_ARTIFACT_REFRESH_SECONDS,
    )


def artifact_live_snapshot(store, catalog, artifact_id: str, version_id: str) -> LiveDataMessage:
    artifact, version = _version(store, artifact_id, version_id)
    if not artifact.live_data_allowed or version.live is None or version.live.invalid_reason:
        return LiveDataMessage(
            snapshots=[],
            refresh_seconds=LIVE_ARTIFACT_REFRESH_SECONDS,
            static=True,
            reason=(
                _LIVE_DISABLED_REASON
                if not artifact.live_data_allowed and version.live is not None
                else version.live.invalid_reason
                if version.live
                else None
            ),
        )
    service = catalog.open(artifact.project_id)
    task, _ = _owner(store, artifact)
    if version.live.graph_branch_id != task.graph_target.branch_id:
        raise ValueError("The artifact's graph target changed.")
    _graph_history(service, task)
    saved = store.read_artifact_live_snapshot(artifact_id, version_id)
    if saved is not None:
        return LiveDataMessage.model_validate_json(saved)
    return _snapshot(store, service, artifact, version)


def _ended(store, live):
    terminal = [binding for binding in live.needs if binding.need.kind in {"job", "episode"}]
    if not terminal:
        return False
    for binding in terminal:
        if binding.need.kind == "job":
            job = store.compute_job(binding.job_id)
            if job is None or job.status == "running":
                return False
        else:
            episode = store.episode(binding.episode_id)
            if episode is None or episode.ended_at is None:
                return False
    return True


def _capture_expired(artifact, now):
    return (
        not artifact.kept_at
        and artifact.expires_at
        and datetime.fromisoformat(artifact.expires_at) <= now
    )


def reconcile_artifact_live_snapshots(store, catalog, project_id: str) -> None:
    """Capture without a viewer; durable, bounded retries survive restarts."""
    now = datetime.fromisoformat(store.now())
    for artifact in store.artifacts(project_id):
        for version in store.artifact_versions(artifact.artifact_id):
            live = version.live
            if (
                not artifact.live_data_allowed
                or _capture_expired(artifact, now)
                or live is None
                or live.invalid_reason
                or version.live_snapshot
                or live.capture_attempts >= LIVE_ARTIFACT_MAX_CAPTURE_ATTEMPTS
                or not any(binding.need.kind in {"job", "episode"} for binding in live.needs)
            ):
                continue
            if live.next_capture_at and datetime.fromisoformat(live.next_capture_at) > now:
                continue
            if not _ended(store, live):
                continue
            original_live = live.model_copy(deep=True)
            snapshot = None
            try:
                service = catalog.open(project_id)
                try:
                    _validate_read(store, service, artifact, version)
                except (ValueError, KeyError) as exc:
                    live.invalid_reason = str(exc)
                    raise
                live.capture_attempts += 1
                # SSH and file reads never hold the lock used by viewer actions.
                snapshot = _snapshot(store, service, artifact, version, final=True)
                if not snapshot.complete:
                    raise ValueError(
                        "; ".join(item.error for item in snapshot.snapshots if item.error)
                    )
                snapshot.final = True
                live.capture_error = None
                live.next_capture_at = None
            except Exception as exc:
                live.capture_error = str(exc)
                # Opening a source can fail before the actual snapshot attempt.
                live.capture_attempts = original_live.capture_attempts + 1
                live.next_capture_at = None
                if (
                    not live.invalid_reason
                    and live.capture_attempts < LIVE_ARTIFACT_MAX_CAPTURE_ATTEMPTS
                ):
                    delay = min(
                        LIVE_ARTIFACT_MAX_RETRY_SECONDS,
                        LIVE_ARTIFACT_RETRY_SECONDS * 2 ** min(live.capture_attempts - 1, 16),
                    )
                    live.next_capture_at = (now + timedelta(seconds=delay)).isoformat()
                logger.warning("Incomplete live artifact capture %s: %s", artifact.artifact_id, exc)
            with store.artifact_lock(artifact.artifact_id):
                try:
                    current_artifact, current = _version(
                        store, artifact.artifact_id, version.version_id
                    )
                except KeyError:
                    continue
                if (
                    current.live_snapshot
                    or current.live != original_live
                    or not current_artifact.live_data_allowed
                    or _capture_expired(current_artifact, datetime.fromisoformat(store.now()))
                ):
                    continue
                if snapshot is not None and snapshot.complete:
                    store.save_artifact_live_snapshot(
                        artifact.artifact_id,
                        version.version_id,
                        data=snapshot.model_dump_json().encode(),
                    )
                store.set_artifact_version_live(artifact.artifact_id, version.version_id, live)
