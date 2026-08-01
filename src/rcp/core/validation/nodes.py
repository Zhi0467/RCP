from __future__ import annotations

import unicodedata
from difflib import SequenceMatcher
from typing import Any

from pydantic import ValidationError

from rcp.core.models import (
    Blocker,
    Decision,
    Evidence,
    Experiment,
    GraphState,
    Hypothesis,
    Patch,
    SourceRef,
)
from rcp.core.ontology import (
    validate_new_node_extensions,
    validate_updated_extension_fields,
)
from rcp.core.validation.constants import NODE_ADAPTER, NODE_PREFIXES, SLUG_RE
from rcp.core.validation.report import ValidationReport


def validate_new_node(
    state: GraphState, patch: Patch, raw: dict[str, Any], report: ValidationReport
) -> None:
    revision = patch.revision or None
    node_id = raw.get("id", "")
    if node_id in state.nodes:
        report.reject(
            "duplicate-node-id", f"Node {node_id!r} already exists; use update_nodes.", revision
        )
    if not SLUG_RE.fullmatch(node_id):
        report.reject("malformed-slug", f"Malformed node slug {node_id!r}.", revision)
    extension_type = raw.get("extension_type")
    expected = extension_type or NODE_PREFIXES.get(raw.get("type"))
    if expected and not node_id.startswith(f"{expected}/"):
        report.reject(
            "wrong-slug-prefix",
            f"Node {node_id!r} must use the {expected}/ prefix for type "
            f"{extension_type or raw.get('type')!r}.",
            revision,
        )
    if raw.get("standing", "asserted") != "asserted" and patch.kind != "approval":
        report.reject(
            "agent-created-trusted-node", "Agent-created nodes must start asserted.", revision
        )
    if raw.get("type") == "blocker" and raw.get("blocker_type") in {"scientific", "design"}:
        report.reject(
            "gated-blocker",
            "Scientific and design blockers must be introduced through a Proposal.",
            revision,
        )
    try:
        NODE_ADAPTER.validate_python(raw)
    except ValidationError as exc:
        report.reject(
            "invalid-node",
            f"Node {node_id!r} is invalid: {exc.errors()[0]['msg']}.",
            revision,
        )
    validate_new_node_extensions(
        state,
        raw,
        report,
        revision,
        authoring=False,
        agent_authored=False,
    )

def validate_new_node_authoring(
    state: GraphState, patch: Patch, raw: dict[str, Any], report: ValidationReport
) -> None:
    revision = patch.revision or None
    node_id = raw.get("id", "")
    node_type = raw.get("type")
    validate_new_node_extensions(
        state,
        raw,
        report,
        revision,
        authoring=True,
        agent_authored=patch.author == "agent",
    )
    if node_type == "evidence" and "origin" not in raw:
        report.reject(
            "missing-evidence-origin",
            f"New Evidence node {node_id!r} must explicitly supply origin.",
            revision,
            related_node_ids=[node_id] if isinstance(node_id, str) else [],
        )
    if node_type == "hypothesis" and raw.get("scope"):
        _validate_grounded_scope(raw, report, revision)

    prefix = node_id.split("/", 1)[0] if "/" in node_id else ""
    suffix = node_id.split("/", 1)[-1]
    for existing in state.nodes:
        existing_prefix, existing_suffix = existing.split("/", 1)
        if (
            prefix == existing_prefix
            and SequenceMatcher(None, suffix, existing_suffix).ratio() >= 0.78
        ):
            report.flag(
                "possible-duplicate",
                f"{node_id} may duplicate {existing}; review rather than merge automatically.",
                revision,
            )
            break


def validate_updated_node_authoring(
    node: Any,
    changes: dict[str, Any],
    report: ValidationReport,
    revision: int | None,
) -> None:
    if not isinstance(node, Hypothesis) or not changes.get("scope"):
        return
    candidate = node.model_dump(mode="python")
    candidate.update(changes)
    _validate_grounded_scope(candidate, report, revision)


def validate_extension_update(
    state: GraphState,
    patch: Patch,
    node: Any,
    changes: dict[str, Any],
    report: ValidationReport,
    *,
    authoring: bool,
) -> None:
    validate_updated_extension_fields(
        state,
        node,
        changes,
        report,
        patch.revision or None,
        authoring=authoring,
        agent_authored=patch.author == "agent",
    )


def _validate_grounded_scope(
    raw: dict[str, Any], report: ValidationReport, revision: int | None
) -> None:
    scope = _normalize_grounding_text(str(raw.get("scope", "")))
    excerpts = [
        _normalize_grounding_text(str(item.get("excerpt", "")))
        for item in raw.get("source_refs", [])
        if isinstance(item, dict)
    ]
    if scope and not any(scope in excerpt for excerpt in excerpts):
        node_id = raw.get("id", "")
        report.reject(
            "ungrounded-hypothesis-scope",
            f"Hypothesis scope for {node_id!r} is not explicitly present in a cited source excerpt.",
            revision,
            related_node_ids=[node_id] if isinstance(node_id, str) else [],
        )


def _normalize_grounding_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def is_gated_update(node: Any, changes: dict[str, Any]) -> bool:
    if node.standing == "accepted" and changes:
        return True
    if isinstance(node, Hypothesis) and "status" in changes and changes["status"] != node.status:
        return True
    if isinstance(node, Decision) and node.status == "open" and changes.get("status") == "decided":
        return True
    if (
        isinstance(node, Evidence)
        and changes.get("validity") in {"invalid", "qualified"}
        and changes.get("validity") != node.validity
    ):
        return True
    if (
        isinstance(node, Experiment)
        and changes.get("status") == "abandoned"
        and node.status != "abandoned"
    ):
        return True
    return (
        isinstance(node, Blocker)
        and node.blocker_type in {"scientific", "design"}
        and bool(changes)
    )


def created_node_id(patch: Patch, node_id: Any) -> bool:
    return any(
        raw.get("id") == node_id
        for op in patch.ops
        if op.get("op") == "create_nodes"
        for raw in op.get("nodes", [])
    )


def oldest_source_ref(raw: dict[str, Any], patch: Patch, report: ValidationReport):
    oldest = None
    run_scope = set(patch.run_truth_scope)
    repositories_read = set(patch.repositories_read)
    for item in raw.get("source_refs", []):
        try:
            ref = SourceRef.model_validate(item)
        except ValidationError as exc:
            report.reject(
                "invalid-source-ref",
                f"Source reference is malformed: {exc.errors()[0]['msg']}.",
                patch.revision or None,
            )
            continue
        if patch.kind != "approval" and ref.truth_repository not in run_scope:
            report.reject(
                "source-outside-run-scope",
                f"Source reference uses {ref.truth_repository!r} outside this run scope.",
                patch.revision or None,
            )
        if patch.kind != "approval" and ref.truth_repository not in repositories_read:
            report.reject(
                "unread-source-repository",
                f"Source reference uses {ref.truth_repository!r}, but the patch did not record reading it.",
                patch.revision or None,
            )
        oldest = older(oldest, ref.timestamp)
    return oldest


def older(left, right):
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)
