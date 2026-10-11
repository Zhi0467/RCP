from __future__ import annotations

from typing import get_args

from rcp.core.operations import GraphOperation, ProposalOperation
from rcp.core.validation.registry import OP_RULES


def test_every_operation_has_a_complete_profile() -> None:
    names = {
        name
        for union in (GraphOperation, ProposalOperation)
        for model in get_args(get_args(union)[0])
        for name in get_args(model.model_fields["op"].annotation)
    }
    assert names == set(OP_RULES)
    for rule in OP_RULES.values():
        assert callable(rule.actions)
        assert callable(rule.apply)
        for check in (rule.structural_validate, rule.authoring_validate, rule.dependencies):
            assert check is None or callable(check)
        assert isinstance(rule.legacy_only, bool)
