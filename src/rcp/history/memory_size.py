"""Account for the Python objects retained by one history generation."""

from __future__ import annotations

import sys
from dataclasses import fields, is_dataclass
from datetime import datetime
from pathlib import PurePath

from pydantic import BaseModel

from rcp.core.validation.report import ValidationReport


def retained_size(root: object) -> int:
    """Count retained models, dataclasses, containers, and immutable leaves once."""
    seen: set[int] = set()
    pending = [root]
    total = 0
    while pending:
        value = pending.pop()
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        total += sys.getsizeof(value)
        if isinstance(value, (str, bytes, int, float, bool, datetime, type(None))):
            continue
        if isinstance(value, PurePath):
            pending.extend(value.parts)
            continue
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
            continue
        if isinstance(value, (list, tuple, set, frozenset)):
            pending.extend(value)
            continue
        if isinstance(value, BaseModel):
            pending.extend(
                (
                    value.__dict__,
                    value.__pydantic_fields_set__,
                    value.__pydantic_extra__,
                    value.__pydantic_private__,
                )
            )
            continue
        if isinstance(value, ValidationReport):
            pending.append(value.__dict__)
        if is_dataclass(value) and not isinstance(value, type):
            namespace = getattr(value, "__dict__", None)
            if namespace is not None:
                pending.append(namespace)
            else:
                pending.extend(getattr(value, field.name) for field in fields(value))
    return total
