"""Saved Paper survives a fresh database, hold resolution, and full history quotas."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest

from rcp import limits
from rcp.paper import PaperLimit, PaperLive, PaperOp
from rcp.storage import AppStore
from rcp.transport import StateUnavailable
from tests.helpers import create_named_app, signed_in_client
from tests.paper_live_fixtures import paper_fixture  # noqa: F401


def _disk(root):
    paper = root / "paper"
    return {
        path.relative_to(paper).as_posix(): path.read_bytes()
        for path in paper.rglob("*")
        if path.is_file()
    }


def test_fresh_database_adopts_the_published_tree(paper, tmp_path):
    live, store, workspace = paper

    async def scenario():
        await live.apply("p", [PaperOp(kind="create", path="introduction.md", text="intro")], "h")
        await live.apply("p", [PaperOp(kind="create", path="body.tex", text="body")], "h")
        await live.apply(
            "p", [PaperOp(kind="create", path="figs/x.png", file_kind="binary", data=b"\x89P")], "h"
        )
        await live.publish("p")
        await live.close()
        published = _disk(workspace.root)
        fresh_store = AppStore(tmp_path / "fresh.sqlite3")
        fresh = PaperLive(fresh_store, lambda _: workspace)
        assert {file.path for file in (await fresh.tree("p")).files} == set(published)
        await fresh.apply("p", [PaperOp(kind="create", path="new.md", text="new")], "h")
        await fresh.publish("p")
        assert (await fresh.tree("p")).publish_state == "published"
        assert _disk(workspace.root) == published | {"new.md": b"new"}
        await fresh.close()
        fresh_store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(("keep", "on_disk"), [("draft", True), ("canonical", False)])
def test_resolving_an_unknown_canonical_keeps_both_texts(paper, keep, on_disk):
    live, store, workspace = paper
    if on_disk:
        (workspace.root / "paper").mkdir()
        (workspace.root / "paper" / "introduction.md").write_text("canonical")
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO paper_drafts(project_id,content,base_hash,updated_at) "
            "VALUES('p','draft','stale','2026-01-01T00:00:00+00:00')"
        )
    snapshot = workspace.read_snapshot
    unreachable = True

    @contextmanager
    def read_snapshot():
        if unreachable:
            raise StateUnavailable("unreachable")
        with snapshot() as root:
            yield root

    workspace.read_snapshot = read_snapshot

    async def scenario():
        nonlocal unreachable
        assert (await live.tree("p")).hold is not None
        unreachable = False
        await live.resolve_hold("p", keep, "h")
        retained = set()
        for version in await live.history("p"):
            retained |= {
                file.text
                for file in (await live.version_snapshot("p", version["version_id"])).files
            }
        retained |= {file.text for file in (await live.snapshot("p")).files}
        assert {"draft", "canonical"} & retained == (
            {"draft", "canonical"} if on_disk else {"draft"}
        )
        await live.close()

    asyncio.run(scenario())


def test_full_history_quota_refuses_only_new_names(paper, monkeypatch):
    live, _, _ = paper

    async def scenario():
        await live.apply("p", [PaperOp(kind="create", path="a.md", text="x" * 2000)], "h")
        await live.create_version("p", "kept", "h")
        monkeypatch.setattr(limits, "PAPER_HISTORY_MAX_BYTES", 10)
        receipt = await live.apply("p", [PaperOp(kind="create", path="b.md")], "h")
        assert receipt.revision == 2
        with pytest.raises(PaperLimit):
            await live.create_version("p", "refused", "h")
        await live.close()

    asyncio.run(scenario())


def test_replacing_content_of_a_missing_file_is_not_found(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    with signed_in_client(app) as client:
        prefix = f"/api/projects/{app.state.default_project_id}/paper"
        response = client.put(
            prefix + "/files/missing/content", files={"file": ("a.md", b"text", "text/markdown")}
        )
        assert response.status_code == 404
