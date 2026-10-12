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
    # OpRule requires actions and apply, so a registered op is a complete profile.
    assert names == set(OP_RULES)
