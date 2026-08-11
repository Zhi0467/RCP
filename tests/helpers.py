from __future__ import annotations

import json
from typing import Any

from rcp.api import create_app
from rcp.core.models import Patch
from rcp.history import HistoryManager

_RCP_OWNED_ITEM_FIELDS = {
    "create_nodes": ("nodes", {"standing", "created_rev", "updated_rev"}),
    "create_edges": ("edges", {"layer", "created_rev"}),
    "create_ambiguities": ("ambiguities", {"raised_rev"}),
    "create_proposals": (
        "proposals",
        {
            "related_node_ids",
            "related_config_keys",
            "base_rev",
            "status",
            "raised_rev",
            "resolved_rev",
            "rejection_reason",
        },
    ),
    "upsert_glossary": ("terms", {"updated_rev"}),
}


def create_named_app(*args: Any, **kwargs: Any):
    """Create an app whose personal test owner has accepted the write precondition."""

    app = create_app(*args, **kwargs)
    if app.state.space_kind == "personal":
        store = app.state.background_tasks.store
        owner = store.local_owner
        if owner is not None and owner.display_name is None:
            store.rename_space_user(owner.user_id, "Test researcher")
    return app


def append_fixture_patch(service: Any, patch: Patch, **kwargs: Any):
    """Prepare canonical graph state without impersonating a production agent task."""

    fixture_history = HistoryManager(service.manifest, service.history.workspace)
    appended, result = fixture_history.append(patch, **kwargs)
    # Mirror the cache update that the production manager would have performed
    # if this test-only legacy fixture had gone through its guarded admission.
    service.history._remember_accepted_revision(result)
    return appended, result


def agent_patch_json(patch: Patch) -> str:
    """Render canonical test data as the semantic JSON an agent may write."""

    operations = json.loads(json.dumps(patch.ops))
    for operation in operations:
        owned = _RCP_OWNED_ITEM_FIELDS.get(operation.get("op"))
        if owned is None:
            continue
        field, excluded = owned
        operation[field] = [
            {key: value for key, value in item.items() if key not in excluded}
            for item in operation.get(field, [])
        ]
    return json.dumps(
        {
            "summary": patch.summary,
            "ops": operations,
            "repositories_read": list(patch.repositories_read),
            "change_summary": list(patch.change_summary),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def refresh_patch(node_id: str = "rq/transfer-after-shift") -> Patch:
    """A minimal refresh patch that applies cleanly on top of ``seed_patch``."""
    return Patch(
        kind="refresh",
        author="agent",
        summary="Recorded a second research question.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "create_nodes",
                "nodes": [
                    {
                        "id": node_id,
                        "type": "research_question",
                        "title": "Transfer after task shift",
                        "question": "Does replanning transfer to an unseen task family?",
                        "motivation": "The seed corpus left transfer unexamined.",
                        "scope": "Matched compute across task families.",
                        "status": "open",
                    }
                ],
            }
        ],
        change_summary=[f"Added {node_id}."],
    )


def shape_invalid_patch() -> Patch:
    """Parses as a Patch, but names an operation the agent schema does not define."""
    return Patch(
        kind="refresh",
        author="agent",
        summary="Used an operation that is not in the agent schema.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[{"op": "invent_nodes", "nodes": []}],
    )


def gated_patch() -> Patch:
    """Well formed, but asks for a transition the graph gates behind a Proposal."""
    return Patch(
        kind="refresh",
        author="agent",
        summary="Tried to bypass a gated transition.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "update_nodes",
                "nodes": [
                    {
                        "id": "hyp/replanning-restores-plasticity",
                        "changes": {"status": "supported"},
                    }
                ],
            }
        ],
    )


def seed_patch() -> Patch:
    return Patch(
        kind="seed",
        author="agent",
        summary="Seeded the project question and initial hypothesis.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "create_nodes",
                "nodes": [
                    {
                        "id": "rq/learning-after-shift",
                        "type": "research_question",
                        "title": "Learning after task shift",
                        "question": "Can the learner retain its ability to adapt after the task changes?",
                        "motivation": "Persistent agents encounter repeated changes.",
                        "scope": "Matched compute and update histories.",
                        "status": "open",
                    },
                    {
                        "id": "hyp/replanning-restores-plasticity",
                        "type": "hypothesis",
                        "title": "Replanning restores plasticity",
                        "statement": "Search-time replanning restores future learning ability.",
                        "rationale": "It may reduce dependence on stale value features.",
                        "predictions": ["The unseen-task learning curve recovers."],
                        "status": "proposed",
                    },
                ],
            },
            {
                "op": "create_edges",
                "edges": [
                    {
                        "source": "rq/learning-after-shift",
                        "target": "hyp/replanning-restores-plasticity",
                        "relation": "has_hypothesis",
                    }
                ],
            },
        ],
    )
