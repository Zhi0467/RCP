from __future__ import annotations

import asyncio
import hashlib
from contextlib import contextmanager

import pytest
from pycrdt import Doc, Text, create_update_message

from rcp import limits
from rcp.paper import PaperConflict, PaperLimit, PaperLive, PaperOp
from rcp.storage import AppStore
from rcp.transport import LocalStateWorkspace, StateUnavailable
from tests.helpers import async_wait_until


@pytest.fixture
def paper(tmp_path):
    store = AppStore(tmp_path / "app.sqlite3")
    root = tmp_path / "state"
    root.mkdir()
    workspace = LocalStateWorkspace(root, str(root))
    live = PaperLive(store, lambda _: workspace)
    yield live, store, workspace
    store.close()


def run(coroutine):
    return asyncio.run(coroutine)


def test_apply_guards_atomicity_unicode_and_receipts(paper):
    live, store, _ = paper

    async def scenario():
        receipt = await live.apply("p", [PaperOp(kind="create", path="a.md", text="a🙂b")], "human")
        file_id = receipt.file_ids[0]
        ops = [PaperOp(kind="replace", file_id=file_id, start=1, end=2, text="é")]
        expect = {"receipt_key": "card-1", "ops": [{"text": "🙂"}]}
        accepted = await live.apply("p", ops, "human", expect)
        assert await live.apply("p", ops, "human", expect) == accepted
        assert await live.text_at("p", file_id, accepted.revision) == "aéb"
        assert await live.text_at("p", file_id, receipt.revision) == "a🙂b"
        with pytest.raises(PaperConflict):
            await live.apply(
                "p",
                [
                    PaperOp(kind="delete", file_id=file_id),
                    PaperOp(kind="create", path="same"),
                    PaperOp(kind="create", path="same"),
                ],
                "human",
            )
        assert (await live.tree("p")).revision == accepted.revision
        with pytest.raises(PaperConflict):
            await live.apply("p", ops, "other", expect)
        with pytest.raises(PaperConflict):
            await live.apply("p", ops, "human", {"ops": [{"text": "stale"}]})
        assert (await live.snapshot("p")).files[0].text == "aéb"
        with store.connection() as conn:
            assert conn.execute("SELECT COUNT(*) FROM paper_change_receipts").fetchone()[0] == 1
        await live.close()

    run(scenario())


def test_commit_failure_never_mutates_or_broadcasts_or_acks(paper, monkeypatch):
    live, store, _ = paper

    async def scenario():
        file_id = (await live.apply("p", [PaperOp(kind="create", path="a")], "h")).file_ids[0]
        room = await live.connect("p", file_id)
        queue = asyncio.Queue()
        room.subscribers.add(queue)
        client = Doc({"text": Text()})
        client.apply_update(room.doc.get_update())
        client["text"].insert(0, "unsaved")
        old = store.connection

        @contextmanager
        def fails_commit():
            with old() as conn:
                yield conn
                if conn.in_transaction:
                    raise OSError("disk failure")

        monkeypatch.setattr(store, "connection", fails_commit)
        with pytest.raises(OSError):
            await live.receive("p", file_id, create_update_message(client.get_update()), "h")
        monkeypatch.setattr(store, "connection", old)
        assert str(room.doc["text"]) == ""
        assert queue.empty()
        assert (await live.snapshot("p")).files[0].text == ""
        assert (await live.tree("p")).revision == 1
        await live.close()

    run(scenario())


def test_restart_recovers_tail_and_publication_and_compaction_cutoff(paper, monkeypatch):
    live, store, workspace = paper
    monkeypatch.setattr(limits, "PAPER_COMPACT_UPDATES", 2)

    async def scenario():
        file_id = (await live.apply("p", [PaperOp(kind="create", path="a.md")], "h")).file_ids[0]
        room = await live.connect("p", file_id)
        client = Doc({"text": Text()})
        client.apply_update(room.doc.get_update())
        client["text"].insert(0, "one")
        ack = await live.receive("p", file_id, create_update_message(client.get_update()), "h")
        assert ack[0][0] == 100
        state = client.get_state()
        client["text"].insert(3, " two")
        await live.receive("p", file_id, create_update_message(client.get_update(state)), "h")
        await live.drain("p")
        with store.connection() as conn:
            assert conn.execute("SELECT through_seq FROM paper_doc_states").fetchone()[0] == 2
            assert [row[0] for row in conn.execute("SELECT seq FROM paper_updates")] == [3]
        await live.close()
        restarted = PaperLive(store, lambda _: workspace)
        recovered = await restarted.connect("p", file_id)
        assert str(recovered.doc["text"]) == "one two"
        assert (await restarted.tree("p")).publish_state == "pending"
        monkeypatch.setattr(limits, "PAPER_TIMER_SECONDS", 0.01)
        monkeypatch.setattr(limits, "PAPER_PUBLISH_IDLE_SECONDS", 0)
        await restarted.start()

        def published():
            with store.connection() as conn:
                row = conn.execute(
                    "SELECT saved_revision,published_revision FROM paper_publication WHERE project_id='p'"
                ).fetchone()
                return row[0] == row[1]

        await async_wait_until(published)
        assert (workspace.root / "paper/a.md").read_text() == "one two"
        assert (await restarted.tree("p")).publish_state == "published"
        await restarted.close()

    run(scenario())


@pytest.mark.parametrize(
    "canonical,draft,base,held",
    [
        ("same", "same", "same", False),
        ("base", "draft", "base", False),
        ("incoming", "draft", "ancestor", True),
    ],
)
def test_legacy_migration_is_idempotent_and_holds_divergence(paper, canonical, draft, base, held):
    live, store, workspace = paper
    (workspace.root / "paper").mkdir()
    (workspace.root / "paper/introduction.md").write_text(canonical)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO paper_drafts(project_id,content,base_hash,ancestor_content,updated_at) "
            "VALUES(?,?,?,?,?)",
            ("p", draft, hashlib.sha256(base.encode()).hexdigest(), base, store.now()),
        )

    async def scenario():
        first = await live.snapshot("p")
        assert first.files[0].text == draft
        tree = await live.tree("p")
        assert bool(tree.hold) is held
        if held:
            await live.publish("p")
            assert (workspace.root / "paper/introduction.md").read_text() == canonical
            history = await live.history("p")
            assert len(history) == 2
            await live.resolve_hold("p", "canonical", "h")
            assert (await live.snapshot("p")).files[0].text == canonical
            assert (await live.tree("p")).hold is None
        await live.close()
        restarted = PaperLive(store, lambda _: workspace)
        assert (await restarted.snapshot("p")).files[0].file_id == first.files[0].file_id
        await restarted.close()

    run(scenario())


def test_server_history_timer_ring_named_quota_and_dedup(paper, monkeypatch):
    live, store, _ = paper
    monkeypatch.setattr(limits, "PAPER_HISTORY_IDLE_SECONDS", 0)
    monkeypatch.setattr(limits, "PAPER_HISTORY_AUTO_LIMIT", 2)
    monkeypatch.setattr(limits, "PAPER_HISTORY_NAMED_LIMIT", 1)

    async def scenario():
        file_id = (
            await live.apply("p", [PaperOp(kind="create", path="a", text="0")], "h")
        ).file_ids[0]
        for index in range(1, 5):
            await live.apply(
                "p",
                [PaperOp(kind="replace", file_id=file_id, start=0, end=1, text=str(index))],
                "h",
                {"ops": [{"text": str(index - 1)}]},
            )
            await live.tick()
        named = await live.create_version("p", "Milestone", "h")
        with pytest.raises(PaperLimit):
            await live.create_version("p", "Too many", "h")
        assert len(await live.history("p")) == 3
        assert (await live.version_snapshot("p", named["version_id"])).files[0].text == "4"
        with store.connection() as conn:
            assert conn.execute("SELECT COUNT(*) FROM paper_version_texts").fetchone()[0] == 5
        await live.close()

    run(scenario())


def test_offline_publication_never_blocks_saving_and_retries(paper, monkeypatch):
    live, _, workspace = paper
    publish = workspace.publish_paper_tree

    def offline(*_):
        raise StateUnavailable("unreachable")

    monkeypatch.setattr(workspace, "publish_paper_tree", offline)

    async def scenario():
        await live.apply("p", [PaperOp(kind="create", path="a", text="saved")], "h")
        await live.publish("p")
        assert (await live.tree("p")).publish_state == "failed"
        await live.apply("p", [PaperOp(kind="create", path="b")], "h")
        monkeypatch.setattr(workspace, "publish_paper_tree", publish)
        await live.publish("p")
        assert (await live.tree("p")).published_revision == 2
        await live.close()

    run(scenario())


def test_worker_loop_marshals_and_pause_fences_new_edits(paper):
    live, _, _ = paper

    async def scenario():
        await live.start()
        result = await asyncio.to_thread(
            lambda: asyncio.run(
                live.apply("p", [PaperOp(kind="create", path="worker.md")], "worker")
            )
        )
        assert result.revision == 1
        await live.pause("p")
        with pytest.raises(PaperConflict):
            await live.apply("p", [PaperOp(kind="create", path="blocked")], "h")
        await live.drain("p")
        await live.dispose("p")
        await live.close()

    run(scenario())


def test_captured_binary_snapshot_survives_retention(paper, tmp_path, monkeypatch):
    live, store, _ = paper
    monkeypatch.setattr(limits, "PAPER_SAVED_REVISION_LIMIT", 1)

    async def scenario():
        file_id = (
            await live.apply(
                "p",
                [PaperOp(kind="create", path="figure.png", file_kind="binary", data=b"old")],
                "h",
            )
        ).file_ids[0]
        snapshot = await live.snapshot("p")
        await live.apply("p", [PaperOp(kind="binary", file_id=file_id, data=b"new")], "h")
        with store.connection() as conn:
            assert conn.execute("SELECT COUNT(*) FROM paper_blobs").fetchone()[0] == 1
        await asyncio.to_thread(live.materialize, snapshot, tmp_path / "captured")
        assert (tmp_path / "captured/figure.png").read_bytes() == b"old"
        await live.close()

    run(scenario())


def test_connect_reserves_room_before_socket_acceptance(paper, monkeypatch):
    live, _, _ = paper
    monkeypatch.setattr(limits, "PAPER_MAX_ROOMS_PER_PROJECT", 1)

    async def scenario():
        ids = (
            await live.apply(
                "p", [PaperOp(kind="create", path="a"), PaperOp(kind="create", path="b")], "h"
            )
        ).file_ids
        queue = asyncio.Queue()
        room = await live.connect("p", ids[0], queue)
        with pytest.raises(PaperLimit):
            await live.connect("p", ids[1], asyncio.Queue())
        assert live.rooms[("p", ids[0])] is room
        room.subscribers.discard(queue)
        assert await live.connect("p", ids[1]) is not room
        await live.close()

    run(scenario())


def test_causal_update_gap_survives_compaction_and_restart(paper, monkeypatch):
    live, store, workspace = paper
    monkeypatch.setattr(limits, "PAPER_COMPACT_UPDATES", 2)

    async def scenario():
        file_id = (await live.apply("p", [PaperOp(kind="create", path="a")], "h")).file_ids[0]
        client = Doc({"text": Text()})
        client["text"].insert(0, "a")
        first, state = client.get_update(), client.get_state()
        client["text"].insert(1, "b")
        second = client.get_update(state)
        await live.receive("p", file_id, create_update_message(second), "h")
        await live.drain("p")
        assert (await live.snapshot("p")).files[0].text == ""
        await live.close()
        restarted = PaperLive(store, lambda _: workspace)
        await restarted.receive("p", file_id, create_update_message(first), "h")
        assert (await restarted.snapshot("p")).files[0].text == "ab"
        assert str((await restarted.connect("p", file_id)).doc["text"]) == "ab"
        await restarted.close()

    run(scenario())


def test_receive_uses_blob_sizes_and_batches_retention(paper, monkeypatch):
    import threading

    import rcp.paper.live as module

    live, store, _ = paper

    async def scenario():
        receipt = await live.apply(
            "p",
            [
                PaperOp(kind="create", path="a.md"),
                PaperOp(kind="create", path="image", file_kind="binary", data=b"image"),
            ],
            "h",
        )
        room = await live.connect("p", receipt.file_ids[0])
        client = Doc({"text": Text()})
        client.apply_update(room.encoded)
        client["text"].insert(0, "saved")
        owner = threading.get_ident()
        queries = []
        connection = store.connection

        @contextmanager
        def traced():
            with connection() as conn:
                conn.set_trace_callback(lambda sql: queries.append(sql))
                yield conn

        original = live._save_text_update

        def save(*args):
            assert threading.get_ident() != owner
            return original(*args)

        with monkeypatch.context() as patch:
            patch.setattr(store, "connection", traced)
            patch.setattr(live, "_save_text_update", save)
            patch.setattr(module, "prune", lambda *_: pytest.fail("per-update retention"))
            assert await live.receive(
                "p", receipt.file_ids[0], create_update_message(client.get_update()), "h"
            )
        assert not any("SELECT bytes" in sql for sql in queries)
        assert (await live.snapshot("p")).files[0].text == "saved"
        await live.tick()
        await live.close()

    run(scenario())
