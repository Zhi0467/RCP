from __future__ import annotations


def is_member(*, seated: bool, project_retired: bool, member_removing: bool) -> bool:
    """A project member holds a seat on a live project and is not being removed."""

    return seated and not project_retired and not member_removing
