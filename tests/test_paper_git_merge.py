from __future__ import annotations

import pytest

from rcp.paper.git_merge import merge_trees
from rcp.paper.git_sync import GitEntry


def tree(body: bytes | None):
    return {} if body is None else {"paper.md": GitEntry(body)}


@pytest.mark.parametrize(
    "base,ours,theirs,expected",
    [
        (b"base", b"ours", b"base", b"ours"),
        (b"base", b"same", b"same", b"same"),
        (b"base", None, None, None),
        (None, None, b"new", b"new"),
        (b"base", b"base", b"new", b"new"),
        (b"base", b"base", None, None),
        (b"a\nb\nc\n", b"A\nb\nc\n", b"a\nb\nC\n", b"A\nb\nC\n"),
        (b"a\nb\nc\n", b"A\nb\nc\n", b"X\nb\nC\n", b"X\nb\nC\n"),
        (b"base\n", b"ours\n", b"theirs\n", b"theirs\n"),
        (None, b"ours", b"theirs", b"theirs"),
        (b"base", b"ours\0", b"theirs", b"theirs"),
        (b"base", b"ours", None, None),
        (b"base", None, b"theirs", b"theirs"),
        (b"base", b"base", b"binary\0", b"binary\0"),
    ],
)
def test_github_wins_every_overlap(base, ours, theirs, expected):
    result = merge_trees(tree(base), tree(theirs), tree(ours))
    assert result.tree == tree(expected)
    assert bool(result.ops) == (ours != expected)


def test_a_write_names_the_bytes_it_read_as_its_base():
    result = merge_trees(
        tree(b"a\nb\nc\n"), tree(b"a\nb\nC\n"), {"paper.md": GitEntry(b"A\nb\nc\n", file_id="f")}
    )
    (op,) = result.ops
    assert (op.kind, op.path, op.base, op.data) == ("write", "paper.md", b"A\nb\nc\n", b"A\nb\nC\n")
    deleted = merge_trees(tree(b"x"), {}, {"paper.md": GitEntry(b"x", file_id="f")})
    assert [(op.kind, op.file_id) for op in deleted.ops] == [("delete", "f")]


def test_invalid_remote_entries_are_skipped():
    theirs = {".git/config": GitEntry(b"bad"), "link": GitEntry(b"target", "120000")}
    result = merge_trees({}, theirs, {})
    assert {item["path"] for item in result.skipped} == set(theirs)
    assert not result.ops


def test_tree_limit_and_collision_skip_in_path_order(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("rcp.limits.PAPER_MAX_FILES", 1)
    result = merge_trees({}, {"a": GitEntry(b"a"), "b": GitEntry(b"b")}, {})
    assert set(result.tree) == {"a"}
    assert [item["path"] for item in result.skipped] == ["b"]
    result = merge_trees({}, {"folder/file": GitEntry(b"remote")}, {"folder": GitEntry(b"local")})
    assert result.skipped[0]["path"] == "folder/file"


@pytest.mark.parametrize(
    "limit", ["PAPER_MAX_UPLOAD_BYTES", "PAPER_MAX_DOC_BYTES", "PAPER_MAX_TREE_BYTES"]
)
def test_remote_limits_skip_new_files(monkeypatch: pytest.MonkeyPatch, limit: str):
    monkeypatch.setattr("rcp.limits." + limit, 2)
    result = merge_trees({}, tree(b"long"), {})
    assert result.skipped[0]["path"] == "paper.md"
    assert not result.ops


def test_collision_checks_every_parent():
    result = merge_trees(
        {}, {"a/b": GitEntry(b"remote")}, {"a": GitEntry(b"local"), "a-1": GitEntry(b"local")}
    )
    assert result.skipped[0]["path"] == "a/b"


def test_an_oversized_merge_takes_githubs_file(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("rcp.limits.PAPER_MAX_DOC_BYTES", 13)
    remote = b"AAAA\n1\n2\n3\nb\n"
    result = merge_trees(tree(b"a\n1\n2\n3\nb\n"), tree(remote), tree(b"A\n1\n2\n3\nBBBB\n"))
    assert result.tree == tree(remote)


def test_later_clean_file_respects_earlier_proposal_budget(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("rcp.limits.PAPER_MAX_TREE_BYTES", 10)
    result = merge_trees(
        {"a": GitEntry(b"base")},
        {"a": GitEntry(b"remote"), "b": GitEntry(b"123456")},
        {"a": GitEntry(b"ours")},
    )
    assert [item["path"] for item in result.skipped] == ["b"]


def test_clean_deletion_makes_room_for_remote_create(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("rcp.limits.PAPER_MAX_FILES", 1)
    base = {"z": GitEntry(b"old")}
    result = merge_trees(base, {"a": GitEntry(b"new")}, base)
    assert result.tree == {"a": GitEntry(b"new")}
    assert not result.skipped
