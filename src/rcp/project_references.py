"""Resolve human-selected project records to immutable attachment input bytes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from rcp.agents.context import ContextAssembler
from rcp.attachments import ProjectReferenceSelector, ProjectReferenceSource
from rcp.core.transition_models import GraphTargetRef
from rcp.limits import (
    CHAT_ATTACHMENT_MAX_FILE_BYTES,
    CHAT_ATTACHMENT_MAX_TOTAL_BYTES,
    PROJECT_REFERENCE_NODE_MAX_BYTES,
)
from rcp.storage import AppStore
from rcp.transport import StateUnavailable

if TYPE_CHECKING:
    from rcp.projects import ProjectCatalog


@dataclass(frozen=True)
class ResolvedProjectReference:
    filename: str
    display_name: str
    media_type: str
    content: bytes
    reference: ProjectReferenceSource


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
                with store.artifact_lock(selector.artifact_id):
                    artifact = store.artifact(selector.artifact_id)
                    if artifact is None or artifact.project_id != project_id:
                        raise ValueError("Project reference artifact is unavailable.")
                    # The same lifetime rule as the artifact routes' _stored_artifact.
                    if (
                        artifact.expires_at is not None
                        and artifact.expires_at <= store.now()
                        and artifact.artifact_id
                        not in (
                            store.protected_edit_artifact_ids() | store.legacy_artifact_import_ids()
                        )
                    ):
                        raise ValueError("Project reference artifact has expired.")
                    # Stored versions may exceed the turn caps; check sizes before reading.
                    size = next(
                        version.size_bytes
                        for version in store.artifact_versions(artifact.artifact_id)
                        if version.version_id == artifact.current_version
                    )
                    artifact_bytes += size
                    if (
                        size > CHAT_ATTACHMENT_MAX_FILE_BYTES
                        or artifact_bytes > CHAT_ATTACHMENT_MAX_TOTAL_BYTES
                    ):
                        raise ValueError(
                            "Project reference artifacts exceed the attachment limits."
                        )
                    content = store.read_artifact_bytes(
                        artifact.artifact_id, artifact.current_version
                    )
                    resolved.append(
                        ResolvedProjectReference(
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
                    )
                continue
            service = catalog.open(project_id)
            if selector.kind == "paper":
                content = service.paper.read_canonical_reference(CHAT_ATTACHMENT_MAX_FILE_BYTES)
                resolved.append(
                    ResolvedProjectReference(
                        filename="introduction.md",
                        display_name="Paper introduction",
                        media_type="text/markdown",
                        content=content,
                        reference=ProjectReferenceSource(
                            kind="paper",
                            source_id="introduction",
                            version=hashlib.sha256(content).hexdigest(),
                        ),
                    )
                )
                continue
            target = GraphTargetRef(
                kind="branch" if selector.branch_id else "main",
                branch_id=selector.branch_id,
            )
            source = service.for_graph_target(target, initialize=False)
            materialization = source.history.current_materialization()
            head = source.history.head_ref(materialization)
            context = ContextAssembler(source.manifest).chat_context(
                materialization.state, node_id=selector.node_id
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
