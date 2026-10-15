from __future__ import annotations


def is_member(*, project_retired: bool, member_removing: bool) -> bool:
    """A seated member counts only on a live project and while not being removed."""

    return not project_retired and not member_removing
