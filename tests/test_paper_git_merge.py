from __future__ import annotations

import pytest

from rcp.paper.git_merge import merge_trees
from rcp.paper.git_sync import GitEntry


def tree(body: bytes | None):
    return {} if body is None else {"paper.md": GitEntry(body)}


@pytest.mark.parametrize(
    "base,ours,theirs,expected,held",
    [
        (b"base", b"ours", b"base", b"ours", None),
        (b"base", b"same", b"same", b"same", None),
        (b"base", None, None, None, None),
        (None, None, b"new", b"new", None),
        (b"base", b"base", b"new", b"new", None),
        (b"base", b"base", None, None, None),
        (b"a\nb\nc\n", b"A\nb\nc\n", b"a\nb\nC\n", b"A\nb\nC\n", None),
        (b"base\n", b"ours\n", b"theirs\n", b"ours\n", "text"),
        (None, b"ours", b"theirs", b"ours", "text"),
        (b"base", b"ours\0", b"theirs", b"ours\0", "file"),
        (b"base", b"ours", None, b"ours", "file"),
        (b"base", None, b"theirs", None, "file"),
        (b"base", b"base", b"binary\0", b"binary\0", None),
    ],
)
def test_merge_table(base, ours, theirs, expected, held):
    result = merge_trees(tree(base), tree(theirs), tree(ours))
    assert result.tree == tree(expected)
    assert [item["kind"] for item in result.held] == ([held] if held else [])
    assert bool(result.ops) == (ours != expected)


def test_invalid_remote_entries_are_skipped():
    theirs = {".git/config": GitEntry(b"bad"), "link": GitEntry(b"target", "120000")}
    result = merge_trees({}, theirs, {})
    assert {item["path"] for item in result.skipped} == set(theirs)
    assert not result.ops


def test_limit_demotion(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("rcp.limits.PAPER_MAX_DOC_BYTES", 8)
    result = merge_trees(tree(b"a\nb\nc\n"), tree(b"a\nb\nCCC\n"), tree(b"AAA\nb\nc\n"))
    assert result.held[0]["kind"] == "file"
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


def test_proposed_conflicts_fit_together(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("rcp.limits.PAPER_MAX_TREE_BYTES", 12)
    base = {"a": GitEntry(b"a\n"), "b": GitEntry(b"b\n")}
    ours = {"a": GitEntry(b"A\n"), "b": GitEntry(b"B\n")}
    theirs = {"a": GitEntry(b"remoteA\n"), "b": GitEntry(b"remoteB\n")}
    result = merge_trees(base, theirs, ours)
    assert [item["kind"] for item in result.held] == ["text", "file"]


def test_binary_base_with_two_text_replacements_is_file_conflict():
    result = merge_trees(tree(b"binary\0"), tree(b"theirs"), tree(b"ours"))
    assert result.held[0]["kind"] == "file"
    assert not result.ops


def test_oversized_theirs_merge_is_demoted(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("rcp.limits.PAPER_MAX_DOC_BYTES", 13)
    result = merge_trees(
        tree(b"a\n1\n2\n3\nb\n"), tree(b"AAAA\n1\n2\n3\nb\n"), tree(b"A\n1\n2\n3\nBBBB\n")
    )
    assert result.held[0]["kind"] == "file"
    assert not result.ops


def test_path_too_long_for_change_store_is_file_conflict():
    path = "/".join(["a" * 255] * 4)
    result = merge_trees(
        {path: GitEntry(b"base")}, {path: GitEntry(b"remote")}, {path: GitEntry(b"local")}
    )
    assert result.held[0]["kind"] == "file"
    assert not result.proposals


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
