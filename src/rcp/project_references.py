"""Resolve human-selected project records to immutable attachment input bytes."""

from __future__ import annotations

import json
import mimetypes
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from rcp.agents.context import ContextAssembler
from rcp.attachments import (
    PaperReferenceSelector,
    ProjectReferenceSelector,
    ProjectReferenceSource,
    _reference_path,
    _regular_tree,
)
from rcp.core.transition_models import GraphTargetRef
from rcp.limits import (
    CHAT_ATTACHMENT_MAX_COUNT,
    CHAT_ATTACHMENT_MAX_FILE_BYTES,
    CHAT_ATTACHMENT_MAX_TOTAL_BYTES,
    PROJECT_REFERENCE_NODE_MAX_BYTES,
)
from rcp.storage import AppStore
from rcp.transport import StateUnavailable

if TYPE_CHECKING:
    from rcp.projects import ProjectCatalog
    from rcp.service import ProjectService


@dataclass(frozen=True)
class ResolvedProjectReference:
    filename: str
    display_name: str
    media_type: str
    content: bytes
    reference: ProjectReferenceSource


def resolve_project_artifact(
    store: AppStore,
    project_id: str,
    artifact_id: str,
    *,
    max_bytes: int = CHAT_ATTACHMENT_MAX_TOTAL_BYTES,
) -> ResolvedProjectReference:
    """Copy the current project artifact under its lock, preserving lifetime and size limits."""
    try:
        with store.artifact_lock(artifact_id):
            artifact = store.readable_artifact(project_id, artifact_id)
            if artifact is None:
                raise ValueError("Project reference artifact is unavailable.")
            size = next(
                version.size_bytes
                for version in store.artifact_versions(artifact.artifact_id)
                if version.version_id == artifact.current_version
            )
            if size > min(CHAT_ATTACHMENT_MAX_FILE_BYTES, max_bytes):
                raise ValueError("Project reference artifacts exceed the attachment limits.")
            content = store.read_artifact_bytes(artifact.artifact_id, artifact.current_version)
            return ResolvedProjectReference(
                filename=PurePosixPath(artifact.source_name).name,
                display_name=artifact.display_title or artifact.source_name,
                media_type=artifact.media_type,
                content=content,
                reference=ProjectReferenceSource(
                    kind="artifact",
                    source_id=artifact.artifact_id,
                    version=artifact.current_version,
                ),
            )
    except (KeyError, OSError, StateUnavailable, StopIteration) as exc:
        raise ValueError("Project reference source is unavailable.") from exc


def resolve_project_references(
    store: AppStore,
    catalog: ProjectCatalog,
    project_id: str,
    selectors: list[ProjectReferenceSelector],
) -> list[ResolvedProjectReference]:
    """Read sources once; attachment admission owns retention and combined caps."""

    keys = [selector.model_dump_json() for selector in selectors]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate project references are not allowed.")
    resolved = []
    artifact_bytes = 0
    for selector in selectors:
        try:
            if selector.kind == "artifact":
                item = resolve_project_artifact(
                    store,
                    project_id,
                    selector.artifact_id,
                    max_bytes=CHAT_ATTACHMENT_MAX_TOTAL_BYTES - artifact_bytes,
                )
                artifact_bytes += len(item.content)
                resolved.append(item)
                continue
            service = catalog.open(project_id)
            if selector.kind == "paper":
                resolved.extend(_resolve_paper(service, project_id, selector))
                continue
            target = GraphTargetRef(
                kind="branch" if selector.branch_id else "main",
                branch_id=selector.branch_id,
            )
            source = service.for_graph_target(target, initialize=False)
            materialization = source.history.current_materialization()
            head = source.history.head_ref(materialization)
            context = ContextAssembler(source.manifest).chat_context(
                materialization.state, graph_target=target, node_id=selector.node_id
            )
            content = json.dumps(
                {
                    "node": context.node,
                    "relations": [item.model_dump(mode="json") for item in context.relations],
                    "graph_head": head.model_dump(mode="json"),
                },
                ensure_ascii=False,
            ).encode("utf-8")
            if len(content) > PROJECT_REFERENCE_NODE_MAX_BYTES:
                raise ValueError("Project reference node snapshot exceeds the byte limit.")
            resolved.append(
                ResolvedProjectReference(
                    filename="node.json",
                    display_name=materialization.state.nodes[selector.node_id].title,
                    media_type="application/json",
                    content=content,
                    reference=ProjectReferenceSource(
                        kind="node", source_id=selector.node_id, graph_head=head
                    ),
                )
            )
        except (KeyError, OSError, StateUnavailable, StopIteration) as exc:
            raise ValueError("Project reference source is unavailable.") from exc
    return resolved


def _resolve_paper(
    service: ProjectService, project_id: str, selector: PaperReferenceSelector
) -> list[ResolvedProjectReference]:
    """Retain one coherent saved tree, including binary files and relative includes."""
    live = service.paper_live
    snapshot = live.run_sync(live.snapshot(project_id))
    files = [item for item in snapshot.files if selector.file_id in (None, item.file_id)]
    if not files:
        raise ValueError("Paper reference source is unavailable.")
    if len(files) > CHAT_ATTACHMENT_MAX_COUNT:
        raise ValueError("The paper reference exceeds the attachment file-count limit.")
    for item in files:
        _reference_path(item.path)
    if len({item.path for item in files}) != len(files):
        raise ValueError("Paper reference paths are not unique.")
    selected = snapshot.model_copy(update={"files": files})
    with tempfile.TemporaryDirectory(prefix="rcp-paper-reference-") as temporary:
        root = Path(temporary) / "paper"
        live.materialize(selected, root)
        paths = _regular_tree(root)
        if set(paths) != {item.path for item in files}:
            raise ValueError("Paper reference snapshot is incomplete.")
        sizes = {path: source.stat().st_size for path, source in paths.items()}
        if any(size > CHAT_ATTACHMENT_MAX_FILE_BYTES for size in sizes.values()):
            raise ValueError("A paper reference exceeds the attachment file-size limit.")
        if sum(sizes.values()) > CHAT_ATTACHMENT_MAX_TOTAL_BYTES:
            raise ValueError("The paper reference exceeds the attachment total-size limit.")
        return [
            ResolvedProjectReference(
                filename=item.path,
                display_name=item.path,
                media_type=(
                    mimetypes.guess_type(item.path)[0]
                    or ("text/plain" if item.kind == "text" else "application/octet-stream")
                ),
                content=paths[item.path].read_bytes(),
                reference=ProjectReferenceSource(
                    kind="paper",
                    source_id=item.file_id,
                    path=item.path,
                    version=str(snapshot.revision),
                ),
            )
            for item in sorted(files, key=lambda item: item.path)
        ]
