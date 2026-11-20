from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from rcp.agents.schema import AgentPatch, agent_output_schema, prepare_agent_patch
from tests.helpers import read_scope_of


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

    patch = prepare_agent_patch(
        draft, kind="work", run_truth_scope=["repo-a"], read_scope=read_scope_of("repo-a")
    )

    assert patch.processed_cursors == {}
