from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rcp.core.materialize import MaterializationResult, apply_valid_patch
from rcp.core.models import AuthorizedHuman, GraphState, Patch, Standing
from rcp.limits import REFRESH_DELTA_MAX_BYTES, REFRESH_DELTA_MAX_ENTRIES

_MAX_TITLE_CHARS = 240
_GRAPH_ID_RE = re.compile(
    r"(?<![a-z0-9_/-])[a-z][a-z0-9]*(?:_[a-z0-9]+)*/"
    r"[a-z0-9]+(?:-[a-z0-9]+)*(?![a-z0-9_/-])"
)
_OPERATION_LABELS = {
    "create_nodes": "added research concepts",
    "update_nodes": "updated research concepts",
    "create_edges": "connected research concepts",
    "remove_edges": "removed graph relationships",
    "remove_nodes": "removed research concepts",
    "supersede_nodes": "superseded research concepts",
    "merge_nodes": "merged research concepts",
    "create_ambiguities": "recorded open questions",
    "resolve_ambiguities": "resolved open questions",
    "create_proposals": "recorded proposals",
    "resolve_proposals": "resolved proposals",
    "withdraw_proposals": "withdrew proposals",
    "upsert_glossary": "updated the glossary",
    "set_coverage": "updated source coverage",
    "set_standing": "updated review standing",
    "set_project_truth_scope": "updated the project truth scope",
    "set_ontology": "updated the project ontology",
}


class RevisionSummary(BaseModel):
    """Deterministic reader-facing prose for one accepted canonical patch."""

    model_config = ConfigDict(extra="forbid")

    from_revision: int = Field(ge=0)
    to_revision: int = Field(ge=1)
    kind: Literal["seed", "refresh", "chat", "work", "experiment_loop", "approval", "identity"]
    author: Literal["agent", "human"] | None
    producer: Literal["agent", "human", "system"]
    authorized_by: AuthorizedHuman | None = None
    profile: Literal["ordinary", "orchestrator"] | None = None
    task_id: str | None = None
    campaign_id: str | None = None
    created_at: str
    sentences: list[str] = Field(min_length=1)


def build_revision_summaries(
    patches: Iterable[Patch],
    materialization: MaterializationResult,
    *,
    from_revision: int = 1,
    to_revision: int | None = None,
) -> list[RevisionSummary]:
    """Render accepted append-only history without exposing graph implementation labels."""

    end = to_revision if to_revision is not None else 10**12
    state = GraphState()
    summaries: list[RevisionSummary] = []

    for patch in sorted(patches, key=lambda item: item.revision):
        report = materialization.reports.get(patch.revision)
        if report is None or report.rejected:
            continue

        previous_state = state
        state = apply_valid_patch(state, patch)
        if not from_revision <= patch.revision <= end:
            continue
        summaries.append(render_revision_summary(previous_state, patch, state))
    return summaries


def render_revision_summary(
    previous_state: GraphState,
    patch: Patch,
    state: GraphState,
) -> RevisionSummary:
    """Render one successfully applied patch without changing either replay state."""

    if patch.kind == "identity" and patch.project_identity is not None:
        home_space_id = patch.project_identity.home_space_id
        if patch.project_identity.action == "created":
            sentences = [f"Project created in {home_space_id}."]
        else:
            sentences = [f"Project identity adopted in {home_space_id}."]
    else:
        labels = _state_labels(previous_state) | _state_labels(state)
        sentences = [
            _plain_history_text(item, labels) for item in patch.change_summary if item.strip()
        ]
        sentences = [item for item in sentences if item]
        if not sentences:
            sentences = _operation_fallbacks(patch, previous_state, state, labels)
        sentences.extend(_proposal_consequence_sentences(patch, state, labels, sentences))
        sentences = _unique_sentences(_plain_history_text(item, labels) for item in sentences)
        if not sentences:
            sentences = ["Recorded a research graph revision."]
    return RevisionSummary(
        from_revision=max(0, patch.revision - 1),
        to_revision=patch.revision,
        kind=patch.kind,
        author=patch.author,
        producer=patch.producer,
        authorized_by=patch.authorized_by,
        profile=patch.profile,
        task_id=patch.task_id,
        campaign_id=patch.campaign_id,
        created_at=patch.created_at.isoformat(),
        sentences=sentences,
    )


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
            elif name == "withdraw_proposals":
                for withdrawal in operation.get("proposals", []):
                    proposal_id = str(withdrawal.get("id", ""))
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
                                | ({"resolution_reason"} if "reason" in withdrawal else set())
                            ),
                            decision="withdrawn",
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


def _state_labels(state: GraphState) -> dict[str, str]:
    return {
        **{node.id: node.title for node in state.nodes.values()},
        **{proposal.id: proposal.title for proposal in state.proposals.values()},
        **{ambiguity.id: ambiguity.question for ambiguity in state.ambiguities.values()},
    }


def _operation_fallbacks(
    patch: Patch,
    previous_state: GraphState,
    state: GraphState,
    labels: dict[str, str],
) -> list[str]:
    sentences: list[str] = []
    for operation in patch.ops:
        name = operation.get("op")
        if name == "create_nodes":
            for node in _dict_items(operation.get("nodes")):
                title = _object_label(str(node.get("id", "")), labels, str(node.get("title", "")))
                noun = str(node.get("extension_type") or node.get("type") or "research concept")
                sentences.append(f"Recorded a {noun.replace('_', ' ')}: {_quoted(title)}.")
        elif name == "update_nodes":
            sentences.extend(
                f"Updated {_quoted(_object_label(str(item.get('id', '')), labels))}."
                for item in _dict_items(operation.get("nodes"))
            )
        elif name == "create_edges":
            for edge in _dict_items(operation.get("edges")):
                source = _object_label(str(edge.get("source", "")), labels)
                target = _object_label(str(edge.get("target", "")), labels)
                sentences.append(f"Connected {_quoted(source)} with {_quoted(target)}.")
        elif name == "remove_edges":
            for edge_id in _string_items(operation.get("edge_ids")):
                edge = previous_state.edges.get(edge_id) or state.edges.get(edge_id)
                if edge is None:
                    sentences.append("Removed a graph relationship.")
                    continue
                sentences.append(
                    f"Removed the relationship between "
                    f"{_quoted(_object_label(edge.source, labels))} and "
                    f"{_quoted(_object_label(edge.target, labels))}."
                )
        elif name == "remove_nodes":
            sentences.extend(
                f"Removed {_quoted(_object_label(node_id, labels))}."
                for node_id in _string_items(operation.get("node_ids"))
            )
        elif name == "supersede_nodes":
            for item in _dict_items(operation.get("nodes")):
                current = _quoted(_object_label(str(item.get("id", "")), labels))
                replacement_id = str(item.get("superseded_by", ""))
                if replacement_id:
                    replacement = _quoted(_object_label(replacement_id, labels))
                    sentences.append(f"Superseded {current} with {replacement}.")
                else:
                    sentences.append(f"Superseded {current}.")
        elif name == "merge_nodes":
            for item in _dict_items(operation.get("merges")):
                duplicate = _quoted(_object_label(str(item.get("duplicate", "")), labels))
                canonical = _quoted(_object_label(str(item.get("canonical", "")), labels))
                sentences.append(f"Merged {duplicate} into {canonical}.")
        elif name == "create_ambiguities":
            for item in _dict_items(operation.get("ambiguities")):
                label = _object_label(
                    str(item.get("id", "")), labels, str(item.get("question", ""))
                )
                sentences.append(f"Recorded an open question: {_quoted(label)}.")
        elif name == "resolve_ambiguities":
            for item in _dict_items(operation.get("resolutions")):
                label = _quoted(_object_label(str(item.get("id", "")), labels))
                verb = "Resolved" if item.get("status") == "resolved" else "Dismissed"
                sentences.append(f"{verb} the open question {label}.")
        elif name == "create_proposals":
            for item in _dict_items(operation.get("proposals")):
                label = _object_label(str(item.get("id", "")), labels, str(item.get("title", "")))
                sentences.append(f"Recorded a proposal: {_quoted(label)}.")
        elif name == "resolve_proposals":
            for item in _dict_items(operation.get("resolutions")):
                label = _quoted(_object_label(str(item.get("id", "")), labels))
                status = str(item.get("status", "resolved")).replace("_", " ").title()
                sentences.append(f"{status} proposal {label}.")
        elif name == "withdraw_proposals":
            for item in _dict_items(operation.get("proposals")):
                label = _quoted(_object_label(str(item.get("id", "")), labels))
                sentences.append(f"Withdrew proposal {label}.")
        elif name == "upsert_glossary":
            sentences.extend(
                f"Updated the glossary entry {_quoted(str(item.get('term', '')).strip())}."
                for item in _dict_items(operation.get("terms"))
                if str(item.get("term", "")).strip()
            )
        elif name == "set_coverage":
            sentences.append("Updated source coverage.")
        elif name == "set_standing":
            label = _quoted(_object_label(str(operation.get("node_id", "")), labels))
            standing = str(operation.get("standing", "asserted"))
            sentences.append(f"Marked {label} {standing}.")
        elif name == "set_project_truth_scope":
            sentences.append("Updated the project truth scope.")
        elif name == "set_ontology":
            sentences.append("Updated the project ontology.")
        else:
            sentences.append("Updated the research graph.")
    return sentences


def _proposal_consequence_sentences(
    patch: Patch,
    state: GraphState,
    labels: dict[str, str],
    existing: list[str],
) -> list[str]:
    proposal_ids: list[str] = []
    for operation in patch.ops:
        name = operation.get("op")
        if name == "create_proposals":
            proposal_ids.extend(
                str(item.get("id", "")) for item in _dict_items(operation.get("proposals"))
            )
        elif name == "resolve_proposals":
            proposal_ids.extend(
                str(item.get("id", ""))
                for item in _dict_items(operation.get("resolutions"))
                if item.get("status") == "approved"
            )

    rendered: list[str] = []
    for proposal_id in dict.fromkeys(proposal_ids):
        proposal = state.proposals.get(proposal_id)
        if proposal is None:
            continue
        title = proposal.title
        consequence = proposal.card.consequences
        plain_consequence = _plain_history_text(consequence, labels)
        if not plain_consequence or any(plain_consequence in item for item in existing):
            continue
        label = _object_label(proposal_id, labels, title)
        rendered.append(
            f"The proposal {_quoted(label)} records this consequence: {_quoted(plain_consequence)}"
        )
    return rendered


def _object_label(identifier: str, labels: dict[str, str], fallback: str = "") -> str:
    return labels.get(identifier) or fallback.strip() or identifier


def _plain_history_text(value: str, labels: dict[str, str]) -> str:
    def replace_identifier(match: re.Match[str]) -> str:
        identifier = match.group(0)
        label = labels.get(identifier)
        return _strip_internal_tokens(label) if label is not None else identifier

    rendered = _GRAPH_ID_RE.sub(replace_identifier, value.strip())
    operation_names = "|".join(re.escape(name) for name in _OPERATION_LABELS)
    rendered = re.sub(
        rf"\s+(?:through|via|using)\s+(?:{operation_names})\b",
        "",
        rendered,
        flags=re.IGNORECASE,
    )
    for operation, label in _OPERATION_LABELS.items():
        rendered = re.sub(rf"\b{re.escape(operation)}\b", label, rendered)
    return " ".join(rendered.split())


def _strip_internal_tokens(value: str) -> str:
    rendered = value
    for operation, label in _OPERATION_LABELS.items():
        rendered = re.sub(rf"\b{re.escape(operation)}\b", label, rendered)
    return " ".join(rendered.split())


def _quoted(value: str) -> str:
    return f"“{_strip_internal_tokens(value)}”"


def _unique_sentences(sentences: Iterable[str]) -> list[str]:
    unique: list[str] = []
    for sentence in sentences:
        if sentence and sentence not in unique:
            unique.append(sentence)
    return unique
