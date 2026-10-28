from __future__ import annotations

import asyncio
import hashlib
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from rcp import limits
from rcp.paper import PaperConflict, PaperLimit, PaperLive, PaperOp
from rcp.storage import AppStore
from rcp.transport import LocalStateWorkspace, StateUnavailable


@pytest.fixture
def paper(tmp_path):
    store = AppStore(tmp_path / "app.sqlite3")
    root = tmp_path / "state"
    root.mkdir()
    workspace = LocalStateWorkspace(root, str(root))
    live = PaperLive(store, lambda _: workspace)
    yield live, store, workspace
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limit,budget",
    [("PAPER_MAX_DOC_BYTES", 4), ("PAPER_MAX_TREE_BYTES", 8), ("PAPER_HISTORY_MAX_BYTES", 12)],
)
async def test_byte_limits_reject_the_entire_mutation(paper, monkeypatch, limit, budget):
    live, store, _ = paper
    initial = await live.apply("p", [PaperOp(kind="create", path="a", text="seed")], "h")
    before = await live.snapshot("p")
    monkeypatch.setattr(limits, limit, budget)
    with pytest.raises(PaperLimit):
        await live.apply(
            "p",
            [
                PaperOp(kind="rename", file_id=initial.file_ids[0], path="renamed"),
                PaperOp(kind="create", path="b", text="abcdefghij"),
            ],
            "h",
            {"receipt_key": "rejected"},
        )
    assert await live.snapshot("p") == before
    with store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM paper_revisions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM paper_files").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM paper_change_receipts").fetchone()[0] == 0
    await live.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("paths", [("../escape",), ("/absolute",), ("a", "a/child")])
async def test_unsafe_and_conflicting_paths_never_enter_the_tree(paper, paths):
    live, _, _ = paper
    with pytest.raises(ValueError):
        await live.apply("p", [PaperOp(kind="create", path=path) for path in paths], "h")
    tree = await live.tree("p")
    assert tree.revision == 0
    assert tree.files == []
    await live.close()


@pytest.mark.asyncio
async def test_project_rate_budget_is_bounded_and_recovers(paper, monkeypatch):
    live, _, _ = paper
    clock = [0.0]
    monkeypatch.setattr("rcp.paper.live.time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(limits, "PAPER_PROJECT_UPDATES_PER_SECOND", 1)
    await live.apply("p", [PaperOp(kind="create", path="a")], "h")
    with pytest.raises(PaperLimit):
        await live.apply("p", [PaperOp(kind="create", path="b")], "h")
    assert (await live.tree("p")).revision == 1
    await live.apply("other", [PaperOp(kind="create", path="a")], "h")
    clock[0] = 1.1
    await live.apply("p", [PaperOp(kind="create", path="b")], "h")
    assert (await live.tree("p")).revision == 2
    await live.close()


@pytest.mark.asyncio
async def test_create_and_unicode_replace_commit_as_one_revision(paper):
    live, _, _ = paper
    receipt = await live.apply(
        "p",
        [
            PaperOp(kind="create", file_id="new", path="a.md", text="a🙂b"),
            PaperOp(kind="replace", file_id="new", start=1, end=2, text="é"),
        ],
        "h",
        {"ops": [{"path_free": "a.md"}, {"text": "🙂"}]},
    )
    assert receipt.revision == 1
    assert await live.text_at("p", "new", 1) == "aéb"
    assert str((await live.connect("p", "new")).doc["text"]) == "aéb"
    await live.close()


@pytest.mark.asyncio
async def test_metadata_and_binary_preconditions_guard_current_content(paper):
    live, _, _ = paper
    created = await live.apply(
        "p", [PaperOp(kind="create", path="figure.png", file_kind="binary", data=b"current")], "h"
    )
    file_id = created.file_ids[0]
    before = await live.snapshot("p")
    for op, condition in (
        (PaperOp(kind="rename", file_id=file_id, path="new.png"), {"path": "stale.png"}),
        (PaperOp(kind="binary", file_id=file_id, data=b"replacement"), {"blob_sha256": "stale"}),
    ):
        with pytest.raises(PaperConflict):
            await live.apply("p", [op], "h", {"ops": [condition]})
        assert await live.snapshot("p") == before
    await live.apply(
        "p",
        [PaperOp(kind="binary", file_id=file_id, data=b"replacement")],
        "h",
        {"ops": [{"blob_sha256": hashlib.sha256(b"current").hexdigest()}]},
    )
    assert live.blob((await live.snapshot("p")).files[0].blob_sha256) == b"replacement"
    await live.close()


@pytest.mark.asyncio
async def test_close_drains_an_already_admitted_legacy_read(paper, monkeypatch):
    live, _, workspace = paper
    entered, release = threading.Event(), threading.Event()
    read_snapshot = workspace.read_snapshot

    @contextmanager
    def blocked():
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test legacy read was not released")
        with read_snapshot() as root:
            yield root

    monkeypatch.setattr(workspace, "read_snapshot", blocked)
    edit = asyncio.create_task(live.apply("p", [PaperOp(kind="create", path="a")], "h"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        closing = asyncio.create_task(live.close())
        await asyncio.sleep(0)
        assert not closing.done()
    finally:
        release.set()
    await asyncio.wait_for(asyncio.gather(edit, closing), timeout=5)
    with pytest.raises(PaperConflict):
        await live.connect("p", edit.result().file_ids[0])


@pytest.mark.asyncio
async def test_offline_legacy_draft_migrates_and_stays_editable(paper, monkeypatch):
    live, store, workspace = paper

    @contextmanager
    def offline():
        raise StateUnavailable("no completed remote generation")
        yield  # pragma: no cover

    monkeypatch.setattr(workspace, "read_snapshot", offline)
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO paper_drafts(project_id,content,base_hash,updated_at) VALUES(?,?,?,?)",
            ("p", "draft", hashlib.sha256(b"base").hexdigest(), store.now()),
        )
    snapshot = await live.snapshot("p")
    assert snapshot.files[0].text == "draft"
    assert (await live.tree("p")).hold is not None
    await live.apply(
        "p",
        [PaperOp(kind="replace", file_id=snapshot.files[0].file_id, start=0, end=5, text="saved")],
        "h",
        {"ops": [{"text": "draft"}]},
    )
    assert (await live.snapshot("p")).files[0].text == "saved"
    await live.close()


@pytest.mark.asyncio
async def test_full_history_quota_never_stops_other_projects_timers(paper, monkeypatch):
    live, store, workspace = paper
    for project in ("full", "other"):
        await live.apply(project, [PaperOp(kind="create", path="a.md", text="seed")], "h")
    await live.create_version("full", "kept", "h")
    monkeypatch.setattr(limits, "PAPER_HISTORY_MAX_BYTES", 1)
    monkeypatch.setattr(limits, "PAPER_PUBLISH_IDLE_SECONDS", 0)
    with pytest.raises(PaperLimit):
        await live.create_version("full", "refused", "h")
    await live.tick()
    await live.drain("full")
    await live.drain("other")
    tree = await live.tree("other")
    assert tree.published_revision == tree.saved_revision
    await live.close()
