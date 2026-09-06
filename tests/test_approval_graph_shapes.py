from __future__ import annotations

import pytest

from rcp.core.models import GraphState, Patch
from rcp.core.validation import validate_patch
from rcp.core.validation.approval import validate_approval_shape
from rcp.core.validation.report import ValidationReport

_CREATE = {"op": "create_edges", "edges": []}
_REMOVE = {"op": "remove_edges", "edge_ids": []}


@pytest.mark.parametrize("ops", [[_CREATE], [_REMOVE], [_REMOVE, _CREATE]])
def test_direct_connection_edit_has_one_named_source_patch(ops) -> None:
    patch = Patch(kind="approval", author="human", summary="Edited connections.", ops=ops)
    report = ValidationReport()
    validate_approval_shape(GraphState(), patch, report, mode="admission")
    assert not report.rejected


@pytest.mark.parametrize(
    "ops",
    [
        [_CREATE, _REMOVE],
        [_CREATE, _CREATE],
        [_REMOVE, _REMOVE],
        [_CREATE, {"op": "set_standing", "node_id": "hyp/example", "standing": "accepted"}],
        [
            _CREATE,
            {
                "op": "resolve_proposals",
                "resolutions": [{"id": "prop/missing", "status": "approved"}],
            },
        ],
    ],
)
def test_connection_edit_does_not_allow_unrelated_or_ambiguous_approval_shapes(ops) -> None:
    patch = Patch(kind="approval", author="human", summary="Mixed changes.", ops=ops)
    report = ValidationReport()
    validate_approval_shape(GraphState(), patch, report, mode="admission")
    assert report.rejected


def test_edge_approval_still_requires_human_authorship() -> None:
    patch = Patch(kind="approval", author="agent", summary="Claimed human action.", ops=[_CREATE])
    report = validate_patch(GraphState(), patch, [])
    assert "wrong-author" in {message.code for message in report.messages}
