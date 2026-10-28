from __future__ import annotations

import asyncio
import hashlib
from contextlib import contextmanager

import pytest
from pycrdt import Doc, Text, create_update_message

from rcp import limits
from rcp.paper import PaperConflict, PaperLimit, PaperLive, PaperOp
from rcp.transport import StateUnavailable
from tests.helpers import async_wait_until
from tests.paper_live_fixtures import paper_fixture  # noqa: F401


def run(coroutine):
    return asyncio.run(coroutine)


def test_apply_guards_atomicity_unicode_and_receipts(paper):
    live, store, _ = paper

    async def scenario():
        receipt = await live.apply("p", [PaperOp(kind="create", path="a.md", text="a🙂b")], "human")
        file_id = receipt.file_ids[0]
        ops = [PaperOp(kind="replace", file_id=file_id, start=1, end=2, text="é")]
        expect = {
            "receipt_key": "card-1",
            "checks": [{"file_id": file_id, "start": 1, "end": 2, "text": "🙂"}],
        }
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
            await live.apply("p", ops, "human", {"checks": [{"file_id": file_id, "text": "stale"}]})
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
                {"checks": [{"file_id": file_id, "text": str(index - 1)}]},
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
        await asyncio.to_thread(live._collect_bodies)
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


def test_repeated_megabyte_deletions_reclaim_crdt_budget(paper, monkeypatch):
    live, store, workspace = paper
    monkeypatch.setattr(limits, "PAPER_COMPACT_UPDATES", 2)

    async def scenario():
        file_id = (await live.apply("p", [PaperOp(kind="create", path="a.md")], "h")).file_ids[0]
        client = Doc({"text": Text()})
        for _ in range(6):
            state = client.get_state()
            client["text"].insert(0, "x" * 1024 * 1024)
            await live.receive("p", file_id, create_update_message(client.get_update(state)), "h")
            state = client.get_state()
            del client["text"][:]
            await live.receive("p", file_id, create_update_message(client.get_update(state)), "h")
        room = await live.connect("p", file_id)
        assert str(room.doc["text"]) == ""
        assert len(room.encoded) < 1024
        await live.close()
        restarted = PaperLive(store, lambda _: workspace)
        room = await restarted.connect("p", file_id)
        assert str(room.doc["text"]) == ""
        assert len(room.encoded) < 1024
        await restarted.close()

    run(scenario())


def test_raw_and_metadata_do_not_read_unrelated_bodies(paper):
    live, store, _ = paper

    async def scenario():
        receipt = await live.apply(
            "p",
            [
                PaperOp(kind="create", path="a.md", text="hello"),
                PaperOp(kind="create", path="b.md", text="unrelated"),
                PaperOp(kind="create", path="image.png", file_kind="binary", data=b"image"),
            ],
            "h",
        )
        # Corrupt unrelated bodies: metadata needs neither, a raw read only its target.
        with store.connection() as conn:
            conn.execute(
                "UPDATE paper_version_texts SET zbody=x'00' WHERE sha256=?",
                (hashlib.sha256(b"unrelated").hexdigest(),),
            )
            conn.execute("UPDATE paper_blobs SET bytes=x'00'")
        tree = await live.tree("p")
        assert [file.size for file in tree.files] == [5, 9, 5]
        for revision in (None, receipt.revision):
            file, data = await live.raw_file("p", receipt.file_ids[0], revision)
            assert file.path == "a.md" and data == b"hello"
        await live.close()

    run(scenario())


def test_runtime_sqlite_and_crdt_rebuilds_run_on_workers(paper, monkeypatch):
    import threading

    import rcp.paper.live as module

    live, store, _ = paper
    owner = threading.get_ident()
    connection, compact = store.connection, module._compact_state

    @contextmanager
    def worker_connection():
        assert threading.get_ident() != owner
        with connection() as conn:
            yield conn

    def worker_compact(encoded):
        assert threading.get_ident() != owner
        return compact(encoded)

    async def scenario():
        monkeypatch.setattr(store, "connection", worker_connection)
        monkeypatch.setattr(module, "_compact_state", worker_compact)
        file_id = (await live.apply("p", [PaperOp(kind="create", path="a.md")], "h")).file_ids[0]
        room = await live.connect("p", file_id)
        client = Doc({"text": Text()})
        client.apply_update(room.encoded)
        client["text"].insert(0, "worker")
        await live.receive("p", file_id, create_update_message(client.get_update()), "h")
        version = await live.create_version("p", "saved", "h")
        await live.publish("p")
        await live.delete_version("p", version["version_id"])
        assert (await live.tree("p")).publish_state == "published"
        monkeypatch.setattr(limits, "PAPER_HISTORY_IDLE_SECONDS", 0)
        live._last_collection -= limits.PAPER_HISTORY_MAX_SECONDS
        await live.tick()
        assert await live.history("p")
        await live.close()

    run(scenario())


@pytest.mark.parametrize("operation", ["apply", "receive"])
def test_cancelled_commit_settles_before_install_and_unlock(paper, monkeypatch, operation):
    import threading

    live, store, _ = paper

    async def scenario():
        file_id = (await live.apply("p", [PaperOp(kind="create", path="a.md")], "h")).file_ids[0]
        room = await live.connect("p", file_id)
        queue = asyncio.Queue()
        room.subscribers.add(queue)
        entered, release = threading.Event(), threading.Event()
        connection = store.connection

        @contextmanager
        def blocked_commit():
            with connection() as conn:
                yield conn
                if conn.in_transaction:
                    entered.set()
                    assert release.wait(5)

        monkeypatch.setattr(store, "connection", blocked_commit)
        if operation == "apply":
            action = live.apply(
                "p",
                [PaperOp(kind="replace", file_id=file_id, start=0, end=0, text="saved")],
                "h",
                {"checks": [{"file_id": file_id, "text": ""}]},
            )
        else:
            client = Doc({"text": Text()})
            client["text"].insert(0, "saved")
            action = live.receive("p", file_id, create_update_message(client.get_update()), "h")
        task = asyncio.create_task(action)
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            assert live._lock("p").locked() and not task.done()
            assert str(room.doc["text"]) == "" and queue.empty()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        monkeypatch.setattr(store, "connection", connection)
        assert str(room.doc["text"]) == "saved" and not queue.empty()
        assert (await live.snapshot("p")).files[0].text == "saved"
        await live.close()

    run(scenario())


def test_failed_publication_waits_for_backoff(paper, monkeypatch):
    live, _, workspace = paper
    attempts = []

    def fail(*args):
        attempts.append(True)
        raise StateUnavailable("offline")

    monkeypatch.setattr(workspace, "publish_paper_tree", fail)
    monkeypatch.setattr(limits, "PAPER_PUBLISH_IDLE_SECONDS", 0)

    async def scenario():
        await live.apply("p", [PaperOp(kind="create", path="a.md")], "h")
        await live.publish("p")
        await live.apply("p", [PaperOp(kind="create", path="b.md")], "h")
        await live.tick()
        await live.drain("p")
        assert len(attempts) == 1
        live._publish_failed_at["p"] -= limits.PAPER_PUBLISH_RETRY_SECONDS
        await live.tick()
        await live.drain("p")
        assert len(attempts) == 2
        await live.close()

    run(scenario())


def test_restore_preserves_peer_position_outside_changed_span(paper):
    live, _, _ = paper

    async def scenario():
        file_id = (
            await live.apply(
                "p", [PaperOp(kind="create", path="a.md", text="prefix old suffix")], "h"
            )
        ).file_ids[0]
        version = await live.create_version("p", "original", "h")
        await live.apply(
            "p",
            [PaperOp(kind="replace", file_id=file_id, start=7, end=10, text="new")],
            "h",
            {"checks": [{"file_id": file_id, "text": "prefix old suffix"}]},
        )
        queue = asyncio.Queue()
        room = await live.connect("p", file_id, queue)
        peer = Doc({"text": Text()})
        peer.apply_update(room.encoded)
        position = peer["text"].sticky_index(3)
        receipt = await live.restore("p", version["version_id"], [file_id], "h")
        from pycrdt import handle_sync_message

        handle_sync_message((await queue.get())[1:], peer)
        assert str(peer["text"]) == "prefix old suffix"
        assert position.get_index() == 3
        unchanged = await live.restore("p", version["version_id"], [file_id], "h")
        assert unchanged.revision == receipt.revision
        await live.close()

    run(scenario())
