from __future__ import annotations

import fnmatch
from pathlib import Path

import pytest

from rcp.agents.hidden_read import HiddenFolderRejected, member_hidden_read_entries
from rcp.limits import HIDDEN_READ_PATH_MAX_COUNT


def test_member_entries_hide_only_other_members_and_escape_glob_roots() -> None:
    directories, globs = member_hidden_read_entries(
        memory_root=Path("/data/[project]/memory"),
        state_root=Path("/data/[project]/state"),
        user_id="alice",
        member_ids=("bob", "alice", "bob", "carol"),
    )
    assert directories == (
        "/data/[project]/memory/members/bob",
        "/data/[project]/memory/members/carol",
    )
    for member, pattern in zip(("bob", "carol"), globs, strict=True):
        assert fnmatch.fnmatchcase(
            f"/data/[project]/state/chat/private-{member}-chat.jsonl", pattern
        )
        assert not fnmatch.fnmatchcase(
            "/data/[project]/state/chat/private-alice-chat.jsonl", pattern
        )
        assert not fnmatch.fnmatchcase(f"/data/p/state/chat/private-{member}-chat.jsonl", pattern)


def test_non_member_run_hides_every_member_and_shares_existing_path_budget() -> None:
    arguments = {
        "memory_root": Path("/memory"),
        "state_root": Path("/state"),
        "user_id": None,
        "member_ids": ("alice", "bob"),
    }
    directories, globs = member_hidden_read_entries(
        **arguments, existing_path_count=HIDDEN_READ_PATH_MAX_COUNT - 4
    )
    assert len(directories) == len(globs) == 2
    with pytest.raises(HiddenFolderRejected) as error:
        member_hidden_read_entries(**arguments, existing_path_count=HIDDEN_READ_PATH_MAX_COUNT - 3)
    assert error.value.code == "too_many_hidden_folders"


@pytest.mark.parametrize("member", ("../alice", "alice/bob", "*", ""))
def test_member_entries_refuse_unsafe_member_ids(member: str) -> None:
    with pytest.raises(ValueError):
        member_hidden_read_entries(
            memory_root=Path("/memory"),
            state_root=Path("/state"),
            user_id=None,
            member_ids=(member,),
        )
