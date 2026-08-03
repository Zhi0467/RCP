from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rcp.core.materialize import MaterializationResult
from rcp.core.models import GraphState, Patch, Standing
from rcp.limits import REFRESH_DELTA_MAX_BYTES, REFRESH_DELTA_MAX_ENTRIES

_MAX_TITLE_CHARS = 240


class RefreshDeltaEntry(BaseModel):
    """Routing metadata for one post-refresh graph or human-authority event."""

    model_config = ConfigDict(extra="forbid")

    category: Literal[
        "current_contested",
        "standing_transition",
        "human_prose_edit",
        "node_removal",
        "chat_graph_update",
        "proposal_decision",
        "ambiguity_decision",
    ]
    target_id: str
    target_type: str
    title: str = Field(default="", max_length=_MAX_TITLE_CHARS)
    revision: int = Field(ge=0)
    author: Literal["agent", "human"]
    field_names: list[str] = Field(default_factory=list)
    previous_standing: Standing | None = None
    current_standing: Standing | None = None
    decision: Literal["approved", "rejected", "withdrawn", "resolved", "dismissed"] | None = None


class RefreshDelta(BaseModel):
    """A deterministic, bounded index of changes since the last graph ingest."""

    model_config = ConfigDict(extra="forbid")

    after_revision: int = Field(ge=0)
    through_revision: int = Field(ge=0)
    entries: list[RefreshDeltaEntry] = Field(max_length=REFRESH_DELTA_MAX_ENTRIES)
    omitted_count: int = Field(ge=0)
    omitted_from_revision: int | None = Field(default=None, ge=0)
    omitted_through_revision: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def enforce_bounds(self) -> RefreshDelta:
        if self.through_revision < self.after_revision:
            raise ValueError("through_revision cannot precede after_revision")
        if _encoded_size(self) > REFRESH_DELTA_MAX_BYTES:
            raise ValueError(f"refresh_delta exceeds {REFRESH_DELTA_MAX_BYTES} bytes")
        return self


def build_refresh_delta(
    patches: Iterable[Patch],
    materialization: MaterializationResult,
) -> RefreshDelta:
    """Build refresh routing data from already-loaded canonical history.

    Rejected patches never enter the delta. Current contested nodes are
    deliberately included even when their transition predates the most recent
    successful seed/refresh, then newer eligible events fill the remaining
    bounded space.
    """

    ordered = sorted(patches, key=lambda item: item.revision)
    accepted = [
        patch
        for patch in ordered
        if patch.revision in materialization.reports
        and not materialization.reports[patch.revision].rejected
    ]
    baseline = max(
        (patch.revision for patch in accepted if patch.kind in {"seed", "refresh"}),
        default=0,
    )
    standing_transitions = _standing_transition_entries(
        accepted,
        materialization.state,
    )
    mandatory = _current_contested_entries(
        materialization.state,
        standing_transitions,
    )
    recent = [
        *(entry for entry in standing_transitions if entry.revision > baseline),
        *_recent_entries(
            (patch for patch in accepted if patch.revision > baseline),
            materialization.state,
        ),
    ]
    recent.sort(
        key=lambda item: (
            -item.revision,
            item.category,
            item.target_type,
            item.target_id,
            tuple(item.field_names),
        )
    )
    mandatory_keys = {_entry_identity(entry) for entry in mandatory}
    candidates = [
        *mandatory,
        *(entry for entry in recent if _entry_identity(entry) not in mandatory_keys),
    ]

    selected: list[RefreshDeltaEntry] = []
    for entry in candidates:
        if len(selected) >= REFRESH_DELTA_MAX_ENTRIES:
            break
        candidate = [*selected, entry]
        omitted = len(candidates) - len(candidate)
        omitted_range = _omitted_revision_range(candidates[len(candidate) :])
        if (
            _candidate_encoded_size(
                after_revision=baseline,
                through_revision=materialization.state.revision,
                entries=candidate,
                omitted_count=omitted,
                omitted_from_revision=omitted_range[0] if omitted_range else None,
                omitted_through_revision=omitted_range[1] if omitted_range else None,
            )
            > REFRESH_DELTA_MAX_BYTES
        ):
            break
        selected.append(entry)

    omitted_entries = candidates[len(selected) :]
    omitted_range = _omitted_revision_range(omitted_entries)
    return RefreshDelta(
        after_revision=baseline,
        through_revision=materialization.state.revision,
        entries=selected,
        omitted_count=len(omitted_entries),
        omitted_from_revision=omitted_range[0] if omitted_range else None,
        omitted_through_revision=omitted_range[1] if omitted_range else None,
    )


def _current_contested_entries(
    state: GraphState,
    transitions: list[RefreshDeltaEntry],
) -> list[RefreshDeltaEntry]:
    entries = []
    for node in sorted(state.nodes.values(), key=lambda item: item.id):
        if node.standing != Standing.CONTESTED:
            continue
        transition = next(
            (
                item
                for item in reversed(transitions)
                if item.target_id == node.id and item.current_standing == Standing.CONTESTED
            ),
            None,
        )
        entries.append(
            RefreshDeltaEntry(
                category="current_contested",
                target_id=node.id,
                target_type=node.type,
                title=_bounded_title(node.title),
                revision=transition.revision if transition is not None else node.updated_rev,
                author=transition.author if transition is not None else "human",
                field_names=["standing"],
                previous_standing=(
                    transition.previous_standing if transition is not None else None
                ),
                current_standing=node.standing,
            )
        )
    return entries


def _standing_transition_entries(
    patches: list[Patch],
    state: GraphState,
) -> list[RefreshDeltaEntry]:
    standings: dict[str, Standing] = {}
    entries: list[RefreshDeltaEntry] = []
    for patch in patches:
        for operation in patch.ops:
            name = operation.get("op")
            if name == "create_nodes":
                for raw in _dict_items(operation.get("nodes")):
                    node_id = str(raw.get("id", ""))
                    if node_id:
                        standings[node_id] = Standing(raw.get("standing", "asserted"))
                continue
            if name == "set_standing":
                node_id = str(operation.get("node_id", ""))
                if not node_id:
                    continue
                before = standings.get(node_id, Standing.ASSERTED)
                after = Standing(str(operation.get("standing")))
                if before != after:
                    entries.append(_standing_entry(state, patch, node_id, before, after))
                standings[node_id] = after
                continue
            if patch.kind == "approval":
                continue
            for node_id in _nodes_reset_by_operation(operation):
                before = standings.get(node_id, Standing.ASSERTED)
                after = Standing.ASSERTED
                if before != after:
                    entries.append(_standing_entry(state, patch, node_id, before, after))
                standings[node_id] = after
    return entries


def _nodes_reset_by_operation(operation: dict[str, object]) -> list[str]:
    name = operation.get("op")
    if name == "update_nodes":
        return [
            str(raw.get("id", "")) for raw in _dict_items(operation.get("nodes")) if raw.get("id")
        ]
    if name == "supersede_nodes":
        return [
            str(raw.get("id", "")) for raw in _dict_items(operation.get("nodes")) if raw.get("id")
        ]
    if name == "merge_nodes":
        return [
            str(raw.get("duplicate", ""))
            for raw in _dict_items(operation.get("merges"))
            if raw.get("duplicate")
        ]
    return []


def _standing_entry(
    state: GraphState,
    patch: Patch,
    node_id: str,
    before: Standing,
    after: Standing,
) -> RefreshDeltaEntry:
    node = state.nodes.get(node_id)
    return RefreshDeltaEntry(
        category="standing_transition",
        target_id=node_id,
        target_type=node.type if node else "node",
        title=_bounded_title(node.title if node else ""),
        revision=patch.revision,
        author=patch.author,
        field_names=["standing"],
        previous_standing=before,
        current_standing=after,
    )


def _recent_entries(
    patches: Iterable[Patch],
    state: GraphState,
) -> list[RefreshDeltaEntry]:
    entries: list[RefreshDeltaEntry] = []
    for patch in patches:
        for operation in patch.ops:
            name = operation.get("op")
            if name == "update_nodes" and patch.kind == "approval":
                for update in operation.get("nodes", []):
                    # Direct literal prose edits carry the optimistic concurrency
                    # guard. Proposal replay operations do not and are routed by
                    # their proposal-decision entry instead.
                    if "base_updated_rev" not in update:
                        continue
                    entries.append(
                        _node_entry(
                            state,
                            patch,
                            "human_prose_edit",
                            str(update.get("id", "")),
                            _field_names(update.get("changes")),
                        )
                    )
            elif name == "resolve_proposals":
                for resolution in operation.get("resolutions", []):
                    decision = resolution.get("status")
                    if decision not in {"approved", "rejected", "withdrawn"}:
                        continue
                    proposal_id = str(resolution.get("id", ""))
                    proposal = state.proposals.get(proposal_id)
                    entries.append(
                        RefreshDeltaEntry(
                            category="proposal_decision",
                            target_id=proposal_id,
                            target_type="proposal",
                            title=_bounded_title(proposal.title if proposal else ""),
                            revision=patch.revision,
                            author=patch.author,
                            field_names=sorted(
                                {"status"}
                                | ({"rejection_reason"} if "reason" in resolution else set())
                            ),
                            decision=decision,
                        )
                    )
            elif name == "resolve_ambiguities" and patch.author == "human":
                for resolution in operation.get("resolutions", []):
                    decision = resolution.get("status")
                    if decision not in {"resolved", "dismissed"}:
                        continue
                    entries.append(
                        RefreshDeltaEntry(
                            category="ambiguity_decision",
                            target_id=str(resolution.get("id", "")),
                            target_type="ambiguity",
                            revision=patch.revision,
                            author=patch.author,
                            field_names=["status"],
                            decision=decision,
                        )
                    )
            elif name == "remove_nodes":
                entries.extend(
                    RefreshDeltaEntry(
                        category="node_removal",
                        target_id=node_id,
                        target_type="node",
                        revision=patch.revision,
                        author=patch.author,
                        field_names=["removed"],
                    )
                    for node_id in _string_items(operation.get("node_ids"))
                )
            if patch.kind in {"chat", "work"} and name != "remove_nodes":
                entries.extend(_chat_entries(patch, operation, state))
    return sorted(
        entries,
        key=lambda item: (
            -item.revision,
            item.category,
            item.target_type,
            item.target_id,
            tuple(item.field_names),
        ),
    )


def _chat_entries(
    patch: Patch,
    operation: dict[str, object],
    state: GraphState,
) -> list[RefreshDeltaEntry]:
    name = str(operation.get("op", ""))
    targets: list[tuple[str, str, str, list[str]]] = []
    if name == "create_nodes":
        for raw in _dict_items(operation.get("nodes")):
            targets.append(
                (
                    str(raw.get("id", "")),
                    str(raw.get("type", "node")),
                    str(raw.get("title", "")),
                    _field_names(raw),
                )
            )
    elif name == "update_nodes":
        for update in _dict_items(operation.get("nodes")):
            node_id = str(update.get("id", ""))
            node = state.nodes.get(node_id)
            targets.append(
                (
                    node_id,
                    node.type if node else "node",
                    node.title if node else "",
                    _field_names(update.get("changes")),
                )
            )
    elif name == "create_edges":
        for raw in _dict_items(operation.get("edges")):
            edge_id = str(
                raw.get("id")
                or f"{raw.get('source', '')}::{raw.get('relation', '')}::{raw.get('target', '')}"
            )
            targets.append((edge_id, "edge", str(raw.get("relation", "")), _field_names(raw)))
    elif name == "remove_edges":
        targets.extend(
            (str(edge_id), "edge", "", ["removed"])
            for edge_id in _string_items(operation.get("edge_ids"))
        )
    elif name in {"supersede_nodes", "merge_nodes"}:
        key = "nodes" if name == "supersede_nodes" else "merges"
        for raw in _dict_items(operation.get(key)):
            node_id = str(raw.get("id") or raw.get("duplicate") or "")
            node = state.nodes.get(node_id)
            targets.append(
                (
                    node_id,
                    node.type if node else "node",
                    node.title if node else "",
                    ["status"],
                )
            )
    elif name == "create_ambiguities":
        targets.extend(
            (str(raw.get("id", "")), "ambiguity", "", _field_names(raw))
            for raw in _dict_items(operation.get("ambiguities"))
        )
    elif name == "resolve_ambiguities":
        targets.extend(
            (str(raw.get("id", "")), "ambiguity", "", ["status"])
            for raw in _dict_items(operation.get("resolutions"))
        )
    elif name == "create_proposals":
        targets.extend(
            (
                str(raw.get("id", "")),
                "proposal",
                str(raw.get("title", "")),
                _field_names(raw),
            )
            for raw in _dict_items(operation.get("proposals"))
        )
    elif name == "upsert_glossary":
        targets.extend(
            (
                str(raw.get("term", "")),
                "glossary_term",
                str(raw.get("term", "")),
                _field_names(raw),
            )
            for raw in _dict_items(operation.get("terms"))
        )
    elif name == "set_project_truth_scope":
        targets.append(("project_truth_scope", "project", "", ["truth_scope"]))

    return [
        RefreshDeltaEntry(
            category="chat_graph_update",
            target_id=target_id,
            target_type=target_type,
            title=_bounded_title(title),
            revision=patch.revision,
            author=patch.author,
            field_names=field_names,
            current_standing=(
                state.nodes[target_id].standing if target_id in state.nodes else None
            ),
        )
        for target_id, target_type, title, field_names in targets
        if target_id
    ]


def _node_entry(
    state: GraphState,
    patch: Patch,
    category: Literal["human_prose_edit"],
    node_id: str,
    field_names: list[str],
) -> RefreshDeltaEntry:
    node = state.nodes.get(node_id)
    return RefreshDeltaEntry(
        category=category,
        target_id=node_id,
        target_type=node.type if node else "node",
        title=_bounded_title(node.title if node else ""),
        revision=patch.revision,
        author=patch.author,
        field_names=field_names,
        current_standing=node.standing if node else None,
    )


def _dict_items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _field_names(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []
    return sorted(str(key) for key in value)


def _entry_identity(entry: RefreshDeltaEntry) -> tuple[str, int, str]:
    return entry.target_id, entry.revision, ",".join(entry.field_names)


def _bounded_title(value: str) -> str:
    return value[:_MAX_TITLE_CHARS]


def _encoded_size(value: RefreshDelta) -> int:
    return len(
        json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _candidate_encoded_size(
    *,
    after_revision: int,
    through_revision: int,
    entries: list[RefreshDeltaEntry],
    omitted_count: int,
    omitted_from_revision: int | None,
    omitted_through_revision: int | None,
) -> int:
    return len(
        json.dumps(
            {
                "after_revision": after_revision,
                "through_revision": through_revision,
                "entries": [entry.model_dump(mode="json") for entry in entries],
                "omitted_count": omitted_count,
                "omitted_from_revision": omitted_from_revision,
                "omitted_through_revision": omitted_through_revision,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _omitted_revision_range(
    entries: list[RefreshDeltaEntry],
) -> tuple[int, int] | None:
    if not entries:
        return None
    revisions = [entry.revision for entry in entries]
    return min(revisions), max(revisions)
