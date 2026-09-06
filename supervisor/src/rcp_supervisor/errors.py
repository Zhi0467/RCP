"""Expected failures reported by the independent supervisor."""

from __future__ import annotations


class SupervisorError(RuntimeError):
    """An actionable supervisor failure, suitable for the operator event stream."""
