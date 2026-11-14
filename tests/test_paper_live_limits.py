from __future__ import annotations

import pytest

from rcp import limits
from rcp.paper import PaperLimit, PaperOp
from tests.paper_live_fixtures import paper_fixture  # noqa: F401


def write(path: str, data: bytes = b"", **fields) -> PaperOp:
    return PaperOp(kind="write", path=path, data=data, **fields)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limit,budget",
    [("PAPER_MAX_DOC_BYTES", 4), ("PAPER_MAX_TREE_BYTES", 8)],
)
async def test_byte_limits_reject_the_entire_write_before_any_file(
    paper, monkeypatch, limit, budget
):
    live, _, root = paper
    initial = await live.apply("p", [write("a", b"seed")], "h")
    revision = (await live.tree("p")).revision
    monkeypatch.setattr(limits, limit, budget)
    with pytest.raises(PaperLimit):
        await live.apply(
            "p",
            [
                PaperOp(kind="rename", file_id=initial.file_ids[0], path="renamed"),
                write("b", b"abcdefghij"),
            ],
            "h",
        )
    assert sorted(path.name for path in (root / "p").iterdir()) == ["a"]
    assert (await live.tree("p")).revision == revision
    await live.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("paths", [("../escape",), ("/absolute",), ("a", "a/child")])
async def test_unsafe_and_conflicting_paths_never_enter_the_folder(paper, paths):
    live, _, root = paper
    with pytest.raises(ValueError):
        await live.apply("p", [write(path) for path in paths], "h")
    assert (await live.tree("p")).files == []
    assert list((root / "p").iterdir()) == []
    await live.close()


@pytest.mark.asyncio
async def test_an_exclusive_create_refuses_an_occupied_path(paper):
    live, _, root = paper
    await live.apply("p", [write("a.md", b"first")], "h")
    with pytest.raises(ValueError):
        await live.apply("p", [write("a.md", b"second", exclusive=True)], "h")
    assert (root / "p/a.md").read_bytes() == b"first"
    await live.close()


@pytest.mark.asyncio
async def test_a_full_history_quota_refuses_only_new_names(paper, monkeypatch):
    live, _, _ = paper
    await live.apply("p", [write("a.md", b"x" * 2000)], "h")
    await live.create_version("p", "kept", "h")
    monkeypatch.setattr(limits, "PAPER_HISTORY_MAX_BYTES", 10)
    await live.apply("p", [write("b.md")], "h", checkpoint=True)
    with pytest.raises(PaperLimit):
        await live.create_version("p", "refused", "h")
    await live.tick()
    await live.close()
