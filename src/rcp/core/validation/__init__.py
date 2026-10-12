"""Patch validation.

Patch-level rules live in :mod:`.patch`; operation-level rules live in
:mod:`.ops` and are declared exactly once in :mod:`.registry`.
"""

from __future__ import annotations

from rcp.core.validation.constants import (
    IDENTIFIER_RE,
    IMMUTABLE_NODE_UPDATE_FIELDS,
    NODE_ADAPTER,
    NODE_PREFIXES,
    SLUG_RE,
)
from rcp.core.validation.context import OpContext, OpRule
from rcp.core.validation.patch import validate_patch
from rcp.core.validation.registry import OP_RULES, proposal_dependencies
from rcp.core.validation.report import ValidationReport
from rcp.core.validation.settings import ValidationContext, build_validation_context

__all__ = [
    "IDENTIFIER_RE",
    "IMMUTABLE_NODE_UPDATE_FIELDS",
    "NODE_ADAPTER",
    "NODE_PREFIXES",
    "OP_RULES",
    "OpContext",
    "OpRule",
    "SLUG_RE",
    "ValidationContext",
    "ValidationReport",
    "build_validation_context",
    "proposal_dependencies",
    "validate_patch",
]
