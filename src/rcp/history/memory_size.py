"""Account for the Python objects retained by one history generation."""

from __future__ import annotations

import sys
from contextlib import suppress
from dataclasses import fields, is_dataclass
from types import CodeType, FunctionType, MethodType, ModuleType

from pydantic import BaseModel


def retained_size(root: object) -> int:
    """Count each reachable instance once, excluding shared class/global machinery."""
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
        if isinstance(
            value, (str, bytes, bytearray, type, ModuleType, FunctionType, MethodType, CodeType)
        ):
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
        namespace = getattr(value, "__dict__", None)
        if isinstance(namespace, dict):
            pending.append(namespace)
        if is_dataclass(value):
            pending.extend(getattr(value, field.name) for field in fields(value))
        for owner in type(value).__mro__:
            slots = owner.__dict__.get("__slots__", ())
            if isinstance(slots, str):
                slots = (slots,)
            for name in slots:
                if name in {"__dict__", "__weakref__"}:
                    continue
                if name.startswith("__") and not name.endswith("__"):
                    name = f"_{owner.__name__.lstrip('_')}{name}"
                with suppress(AttributeError):
                    pending.append(getattr(value, name))
    return total
