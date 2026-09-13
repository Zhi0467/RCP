from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from rcp.agents.prompts import PromptFactory
from rcp.agents.schema import AgentPatch, agent_output_schema, prepare_agent_patch

_VALIDATOR_COMMAND = (
    "python3 /run/inputs/validator.py /run/workspace/patch.json token 30 /run/workspace"
)


def test_contract_names_direct_provider_roots_and_project_watermark() -> None:
    watermark = datetime(2026, 7, 31, 14, 0, tzinfo=UTC)
    contract = PromptFactory.graph_task_contract(
        "refresh",
        project_name="Recovered project",
        ontology_path="/state/graph.json#ontology",
        ontology_extensions=True,
        graph_path="/state/graph.json",
        research_path="/state/research.md",
        provider_log_roots={
            "claude": [
                "/remote/home/.claude/projects",
                "/remote/archive/.claude/projects",
            ],
            "codex": ["/remote/home/.codex/sessions"],
        },
        ingestion_watermark=watermark,
        repositories=[{"alias": "repo-a", "host": "gpu.example", "path": "/remote/project"}],
        patch_path="/run/workspace/patch.json",
        output_schema_path="/run/inputs/patch-schema.json",
        validator_command=_VALIDATOR_COMMAND,
        human_request_path="/run/inputs/human-request.txt",
        source_errors=["claude root is not readable"],
    )

    assert "/remote/home/.claude/projects" in contract
    assert "/remote/archive/.claude/projects" in contract
    assert "/remote/home/.codex/sessions" in contract
    assert "2026-07-31T14:00:00+00:00" in contract
    assert "gpu.example" in contract and "/remote/project" in contract
    assert "/run/inputs/human-request.txt" in contract
    assert "/run/inputs/patch-schema.json" in contract
    assert "/run/workspace/patch.json" in contract
    assert _VALIDATOR_COMMAND in contract
    assert "claude root is not readable" in contract


def test_agent_patch_schema_has_no_coverage_or_cursor_operation() -> None:
    rendered = json.dumps(agent_output_schema())

    assert '"set_coverage"' not in rendered
    assert "CoverageBoundary" not in rendered
    assert "processed_cursors" not in rendered
    with pytest.raises(ValidationError):
        AgentPatch.model_validate(
            {
                "summary": "Claimed source coverage.",
                "ops": [{"op": "set_coverage", "coverage": {"sessions_read": []}}],
            }
        )


def test_prepare_agent_patch_always_emits_empty_processed_cursors() -> None:
    draft = AgentPatch(summary="Recorded direct source findings.", ops=[])

    patch = prepare_agent_patch(draft, kind="refresh", run_truth_scope=["repo-a"])

    assert patch.processed_cursors == {}
