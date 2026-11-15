from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from pycrdt import Doc, Text, create_update_message, handle_sync_message

from rcp.paper import PaperOp
from rcp.paper.folder_io import write as folder_write
from rcp.paper.models import PaperWriteFailed, digest
from tests.paper_live_fixtures import paper_fixture  # noqa: F401


def run(coroutine):
    return asyncio.run(coroutine)


def foreign(root: Path, path: str, text: str | bytes) -> None:
    """An agent's tool writing the folder, a little later than the editor's last look."""
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(text.encode() if isinstance(text, str) else text)
    info = target.stat()
    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000))


async def create(live, path: str, text: str = "", project: str = "p") -> str:
    receipt = await live.apply(
        project, [PaperOp(kind="write", path=path, data=text.encode(), exclusive=True)], "h"
    )
    return receipt.file_ids[0]


class Client:
    """A browser tab: its own Yjs doc, synced to a room."""

    def __init__(self, live, file_id: str, project: str = "p"):
        self.live, self.file_id, self.project = live, file_id, project
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.doc = Doc({"text": Text()})

    async def join(self):
        room = await self.live.connect(self.project, self.file_id, self.queue)
        self.doc.apply_update(room.doc.get_update())
        return room

    def drain(self) -> list[bytes]:
        messages = []
        while not self.queue.empty():
            message = self.queue.get_nowait()
            messages.append(message)
            if message[:2] == b"\x00\x02":
                handle_sync_message(message[1:], self.doc)
        return messages

    async def type(self, index: int, text: str) -> list[bytes]:
        before = self.doc.get_state()
        self.doc["text"].insert(index, text)
        update = self.doc.get_update(before)
        return await self.live.receive(
            self.project, self.file_id, create_update_message(update), "h", self.queue
        )

    @property
    def text(self) -> str:
        return str(self.doc["text"])


def test_ack_only_after_the_buffer_is_on_disk(paper):
    live, _, root = paper

    async def scenario():
        file_id = await create(live, "a.md", "hello")
        client = Client(live, file_id)
        room = await client.join()
        replies = await client.type(5, " world")
        assert replies == []
        assert (root / "p/a.md").read_text() == "hello"
        assert not any(message[:1] == b"\x64" for message in client.drain())
        await live.flush("p")
        assert (root / "p/a.md").read_text() == "hello world"
        acks = [message for message in client.drain() if message[:1] == b"\x64"]
        assert acks == [bytes([100]) + room.doc.get_state()]
        await live.close()

    run(scenario())


def test_a_room_loads_its_cache_when_disk_matches_and_disk_when_it_changed(paper):
    live, store, root = paper
    vectors: list[bytes] = []

    async def first():
        file_id = await create(live, "a.md", "one")
        client = Client(live, file_id)
        room = await client.join()
        await client.type(3, " two")
        await live.flush("p")
        vectors.append(room.doc.get_state())
        await live.close()

    async def reopen(expected: str) -> bytes:
        from rcp.paper import PaperLive
        from rcp.paper.folder import LocalPaperFolder

        again = PaperLive(store, lambda project: LocalPaperFolder(root / project))
        room = await Client(again, file_id_of(store)).join()
        assert room.text == expected
        with store.connection() as conn:
            sha = conn.execute("SELECT text_sha256 FROM paper_doc_states").fetchone()[0]
        assert sha == digest(expected.encode())
        await again.close()
        return room.doc.get_state()

    run(first())
    assert run(reopen("one two")) == vectors[0]
    (root / "p/a.md").write_text("one two three")
    assert run(reopen("one two three")) != vectors[0]


def file_id_of(store, path: str = "a.md") -> str:
    with store.connection() as conn:
        return conn.execute(
            "SELECT file_id FROM paper_files WHERE path=? AND deleted_at IS NULL", (path,)
        ).fetchone()[0]


def test_saves_and_changed_scans_bump_the_revision(paper):
    live, _, root = paper

    async def scenario():
        await create(live, "a.md", "x")
        first = (await live.tree("p")).revision
        assert not await live.poll("p", full=True)
        assert (await live.tree("p")).revision == first
        foreign(root / "p", "b.md", "agent")
        assert await live.poll("p", full=True)
        second = (await live.tree("p")).revision
        assert second == first + 1
        file_id = file_id_of(live.store)
        client = Client(live, file_id)
        await client.join()
        await client.type(1, "y")
        await live.flush("p")
        assert (await live.tree("p")).revision == second + 1
        await live.close()

    run(scenario())


def test_a_foreign_write_merges_into_an_open_buffer_and_the_human_wins_overlaps(paper):
    live, _, root = paper

    async def scenario():
        file_id = await create(live, "a.md", "alpha\nbeta\ngamma\n")
        client = Client(live, file_id)
        await client.join()
        await client.type(0, "HUMAN ")
        foreign(root / "p", "a.md", "alpha\nbeta\ngamma\nagent line\n")
        assert await live.poll("p", full=True)
        client.drain()
        assert client.text == "HUMAN alpha\nbeta\ngamma\nagent line\n"
        assert (root / "p/a.md").read_text() == client.text
        foreign(root / "p", "a.md", "AGENT alpha\nbeta\ngamma\nagent line\n")
        await client.type(0, "x")
        await live.poll("p", full=True)
        client.drain()
        assert client.text.startswith("xHUMAN alpha")
        await live.close()

    run(scenario())


def test_a_slow_agent_write_merges_against_its_previous_write_in_the_turn(paper):
    live, store, root = paper

    async def scenario():
        file_id = await create(live, "a.md", "one\ntwo\nthree\nfour\n")
        await live.begin_turn("p", "op-1")
        foreign(root / "p", "a.md", "one\ntwo\nthree\nfour\nAGENT1\n")
        await live.poll("p", full=True)
        client = Client(live, file_id)
        await client.join()
        await client.type(0, "HUMAN ")
        foreign(root / "p", "a.md", "ONE\ntwo\nthree\nfour\nAGENT1\n")
        await live.poll("p", full=True)
        client.drain()
        assert client.text == "HUMAN one\ntwo\nthree\nfour\nAGENT1\n"
        foreign(root / "p", "a.md", "one\ntwo\nthree\nFOUR\nAGENT1\n")
        await live.poll("p", full=True)
        client.drain()
        assert client.text == "HUMAN one\ntwo\nthree\nFOUR\nAGENT1\n"
        turn = await live.end_turn("p", "op-1")
        (row,) = turn["files"]
        assert row["path"] == "a.md" and row["clash"] is True
        assert turn["before_version_id"] != turn["after_version_id"]
        with store.connection() as conn:
            assert conn.execute("SELECT turn_operation_id FROM paper_state").fetchone()[0] is None
        await live.close()

    run(scenario())


def test_a_rename_keeps_the_file_id(paper):
    live, _, root = paper

    async def scenario():
        file_id = await create(live, "a.md", "content")
        os.rename(root / "p/a.md", root / "p/b.md")
        await live.poll("p", full=True)
        (file,) = (await live.tree("p")).files
        assert (file.path, file.file_id) == ("b.md", file_id)
        await live.close()

    run(scenario())


def test_a_rename_to_another_kind_takes_the_new_kind(paper):
    live, _, root = paper

    async def scenario():
        file_id = await create(live, "draft.md", "content")
        os.rename(root / "p/draft.md", root / "p/draft.pdf")
        await live.poll("p", full=True)
        await live.poll("p", full=True)
        (file,) = (await live.tree("p")).files
        assert (file.path, file.file_id, file.kind) == ("draft.pdf", file_id, "binary")
        await live.close()

    run(scenario())


def test_a_foreign_delete_of_an_edited_open_file_recreates_it_as_a_clash(paper):
    live, _, root = paper

    async def scenario():
        file_id = await create(live, "a.md", "base")
        await live.begin_turn("p", "op-1")
        client = Client(live, file_id)
        await client.join()
        await client.type(4, " typed")
        (root / "p/a.md").unlink()
        await live.poll("p", full=True)
        assert (root / "p/a.md").read_text() == "base typed"
        turn = await live.end_turn("p", "op-1")
        assert turn["files"] == [
            {
                "path": "a.md",
                "kind": "delete",
                "clash": True,
                "base_sha": digest(b"base"),
                "coach_sha": None,
            }
        ]
        await live.close()

    run(scenario())


def test_a_foreign_delete_of_an_unedited_file_is_accepted_and_listed_next_turn(paper):
    live, _, root = paper

    async def scenario():
        await create(live, "a.md", "base")
        (root / "p/a.md").unlink()
        await live.poll("p", full=True)
        assert (await live.tree("p")).files == []
        await live.begin_turn("p", "op-1")
        turn = await live.end_turn("p", "op-1")
        assert [(row["path"], row["kind"]) for row in turn["files"]] == [("a.md", "outside_turn")]
        await live.close()

    run(scenario())


def test_a_binary_upload_during_the_turn_beats_the_agents_bytes(paper):
    live, _, root = paper

    async def scenario():
        await create(live, "a.png", "")
        await live.begin_turn("p", "op-1")
        await live.apply("p", [PaperOp(kind="write", path="a.png", data=b"\x89human")], "h")
        foreign(root / "p", "a.png", b"\x89agent")
        await live.poll("p", full=True)
        assert (root / "p/a.png").read_bytes() == b"\x89human"
        turn = await live.end_turn("p", "op-1")
        assert turn["files"][0]["clash"] is True
        await live.close()

    run(scenario())


def test_a_multi_file_write_runs_in_path_order_reports_a_failure_and_reruns(paper, monkeypatch):
    live, _, root = paper
    order: list[str] = []
    real_write = folder_write

    def write(folder_root, path, data, expect_sha, **settle):
        order.append(path)
        if path == "b.md" and fail[0]:
            raise OSError("disk full")
        return real_write(folder_root, path, data, expect_sha, **settle)

    fail = [True]
    monkeypatch.setattr("rcp.paper.folder_io.write", write)
    ops = [
        PaperOp(kind="write", path=name, data=name.encode()) for name in ("c.md", "a.md", "b.md")
    ]

    async def scenario():
        with pytest.raises(PaperWriteFailed) as failure:
            await live.apply("p", ops, "h")
        assert failure.value.written == ["a.md"]
        assert order == ["a.md", "b.md"]
        assert not (root / "p/c.md").exists()
        fail[0] = False
        await live.apply("p", ops, "h")
        await live.apply("p", ops, "h")
        assert {path.name: path.read_text() for path in (root / "p").iterdir()} == {
            "a.md": "a.md",
            "b.md": "b.md",
            "c.md": "c.md",
        }
        await live.close()

    run(scenario())


def test_an_operation_wins_overlaps_but_keystrokes_after_its_read_survive(paper):
    live, _, root = paper

    async def scenario():
        file_id = await create(live, "a.md", "one\ntwo\nthree\n")
        client = Client(live, file_id)
        await client.join()
        read = b"one\ntwo\nthree\n"
        await client.type(len("one\ntwo\nthree\n"), "typed\n")
        await live.apply(
            "p", [PaperOp(kind="write", path="a.md", base=read, data=b"ONE\ntwo\nthree\n")], "h"
        )
        client.drain()
        assert client.text == "ONE\ntwo\nthree\ntyped\n"
        assert (root / "p/a.md").read_text() == client.text
        await live.close()

    run(scenario())


def test_restore_writes_files_and_keeps_peer_positions_outside_the_change(paper):
    live, _, root = paper

    async def scenario():
        file_id = await create(live, "a.md", "prefix old suffix")
        version = await live.create_version("p", "original", "h")
        foreign(root / "p", "a.md", "prefix new suffix")
        await live.poll("p", full=True)
        client = Client(live, file_id)
        await client.join()
        position = client.doc["text"].sticky_index(3)
        await live.restore("p", version["version_id"], [file_id], "h")
        client.drain()
        assert client.text == "prefix old suffix"
        assert (root / "p/a.md").read_text() == "prefix old suffix"
        assert position.get_index() == 3
        await live.close()

    run(scenario())


def test_restoring_a_deleted_file_keeps_its_id(paper):
    live, _, _root = paper

    async def scenario():
        file_id = await create(live, "a.md", "kept")
        version = await live.create_version("p", "original", "h")
        await live.apply("p", [PaperOp(kind="delete", file_id=file_id)], "h")
        await live.restore("p", version["version_id"], [file_id], "h")
        (file,) = (await live.tree("p")).files
        assert (file.path, file.file_id) == ("a.md", file_id)
        await live.close()

    run(scenario())


def test_restoring_a_deleted_file_over_a_new_occupant_is_refused(paper):
    from rcp.paper.models import PaperPrecondition

    live, _, root = paper

    async def scenario():
        file_id = await create(live, "a.md", "archived")
        version = await live.create_version("p", "original", "h")
        await live.apply("p", [PaperOp(kind="delete", file_id=file_id)], "h")
        occupant = await create(live, "a.md", "occupant")
        with pytest.raises(PaperPrecondition):
            await live.restore("p", version["version_id"], [file_id], "h")
        (file,) = (await live.tree("p")).files
        assert file.file_id == occupant
        assert (root / "p/a.md").read_text() == "occupant"
        await live.close()

    run(scenario())


def test_a_poll_waits_for_a_stable_stat_and_reports_unsupported_entries(paper):
    live, _, root = paper

    async def scenario():
        await create(live, "a.md", "x")
        foreign(root / "p", "b.md", "agent")
        (root / "p/link.md").symlink_to(root / "p/a.md")
        assert await live.poll("p")
        tree = await live.tree("p")
        assert {file.path: file.kind for file in tree.files} == {
            "a.md": "text",
            "link.md": "unsupported",
        }
        await live.poll("p")
        assert "b.md" in {file.path for file in (await live.tree("p")).files}
        await live.close()

    run(scenario())


def _restarted(store, root):
    from rcp.paper import PaperLive
    from rcp.paper.folder import LocalPaperFolder

    return PaperLive(store, lambda project: LocalPaperFolder(root / project))


def test_a_human_edit_saved_in_a_turn_survives_an_agent_write_after_a_restart(paper):
    live, store, root = paper

    async def first():
        file_id = await create(live, "a.md", "one\ntwo\nthree\n")
        await live.begin_turn("p", "op-1")
        client = Client(live, file_id)
        await client.join()
        await client.type(0, "HUMAN ")
        await live.leave("p", file_id, client.queue)
        await live.close()

    async def second():
        again = _restarted(store, root)
        await again.tree("p")
        foreign(root / "p", "a.md", "one\ntwo\nTHREE\n")
        await again.poll("p", full=True)
        await again.close()

    run(first())
    run(second())
    assert (root / "p/a.md").read_text() == "HUMAN one\ntwo\nTHREE\n"


def test_agent_writes_made_while_rcp_was_down_join_the_turn_list(paper):
    live, store, root = paper

    async def first():
        await create(live, "a.md", "one\n")
        await create(live, "b.md", "human\n")
        await live.begin_turn("p", "op-1")
        await live.apply("p", [PaperOp(kind="write", path="b.md", data=b"human saved\n")], "h")
        foreign(root / "p", "a.md", "one\nagent\n")
        await live.close()

    async def second():
        again = _restarted(store, root)
        turn = await again.end_turn("p", "op-1")
        await again.close()
        return turn

    run(first())
    turn = run(second())
    assert [(row["path"], row["kind"]) for row in turn["files"]] == [("a.md", "write")]
    assert turn["files"][0]["coach_sha"] == digest(b"one\nagent\n")


def test_the_timer_watches_a_turn_the_previous_process_left_running(paper):
    live, store, root = paper

    async def first():
        await create(live, "a.md", "one\n")
        await live.begin_turn("p", "op-1")
        await live.close()

    async def second():
        again = _restarted(store, root)
        await again.tick()
        loaded = "p" in again.projects
        await again.close()
        return loaded

    run(first())
    assert run(second())


def test_an_outside_turn_write_is_never_the_next_turns_merge_base(paper):
    live, _, root = paper

    async def scenario():
        file_id = await create(live, "a.md", "l1\nl2\nl3\n")
        foreign(root / "p", "a.md", "l1\nl2\nl3\nl4\n")
        await live.poll("p", full=True)
        client = Client(live, file_id)
        await client.join()
        client.drain()
        start = client.text.index("l2\n")
        before = client.doc.get_state()
        del client.doc["text"][start : start + 3]
        await live.receive(
            "p", file_id, create_update_message(client.doc.get_update(before)), "h", client.queue
        )
        await live.flush("p")
        await live.begin_turn("p", "op-1")
        foreign(root / "p", "a.md", "L1\nl3\nl4\n")
        await live.poll("p", full=True)
        await live.close()

    run(scenario())
    assert (root / "p/a.md").read_text() == "L1\nl3\nl4\n"


def test_outside_turn_writes_keep_one_row_per_path(paper):
    live, store, root = paper

    async def scenario():
        await create(live, "a.md", "zero\n")
        for text in ("one\n", "two\n", "three\n"):
            foreign(root / "p", "a.md", text)
            await live.poll("p", full=True)
        await live.begin_turn("p", "op-1")
        turn = await live.end_turn("p", "op-1")
        await live.close()
        return turn

    turn = run(scenario())
    with store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM paper_turn_files").fetchone()[0] == 1
    (row,) = turn["files"]
    assert (row["kind"], row["coach_sha"]) == ("outside_turn", digest(b"three\n"))


def test_body_collection_keeps_what_rcp_wrote_in_a_running_turn(paper):
    from rcp.paper.persistence import collect_bodies

    live, store, root = paper

    async def first():
        await create(live, "a.md", "one\ntwo\nthree\n")
        await live.begin_turn("p", "op-1")
        await live.apply(
            "p", [PaperOp(kind="write", path="a.md", data=b"one\ntwo\nthree\nmine\n")], "h"
        )
        await live.close()

    run(first())
    with store.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        collect_bodies(conn)

    async def second():
        again = _restarted(store, root)
        await again.tree("p")
        foreign(root / "p", "a.md", "ONE\ntwo\nthree\n")
        await again.poll("p", full=True)
        await again.close()

    run(second())
    assert (root / "p/a.md").read_text() == "ONE\ntwo\nthree\nmine\n"


def test_a_rename_seen_by_timed_polls_keeps_the_file_id(paper):
    live, _, root = paper

    async def scenario():
        file_id = await create(live, "a.md", "hello\n")
        await Client(live, file_id).join()
        await live.poll("p")
        os.rename(root / "p/a.md", root / "p/b.md")
        await live.poll("p")
        await live.poll("p")
        tree = await live.tree("p")
        await live.close()
        return file_id, [(file.path, file.file_id) for file in tree.files]

    file_id, files = run(scenario())
    assert files == [("b.md", file_id)]


def test_a_tracked_file_replaced_by_a_symlink_shows_as_unsupported(paper, tmp_path):
    live, _, root = paper

    async def scenario():
        file_id = await create(live, "a.md", "base")
        client = Client(live, file_id)
        await client.join()
        await client.type(4, " typed")
        target = tmp_path / "outside.md"
        target.write_text("outside")
        (root / "p/a.md").unlink()
        (root / "p/a.md").symlink_to(target)
        await live.poll("p", full=True)
        tree = await live.tree("p")
        await live.close()
        return [(file.path, file.kind) for file in tree.files]

    assert run(scenario()) == [("a.md", "unsupported")]
    assert (tmp_path / "outside.md").read_text() == "outside"


def test_a_file_over_the_read_bound_is_never_read_or_stored(paper, monkeypatch):
    from rcp import limits

    live, store, root = paper
    monkeypatch.setattr(limits, "PAPER_MAX_UPLOAD_BYTES", 64)

    async def scenario():
        await create(live, "a.md", "base")
        await live.begin_turn("p", "op-1")
        (root / "p/checkpoint.bin").write_bytes(b"x" * 65)
        (root / "p/a.md").write_text("agent")
        await live.poll("p", full=True)
        await live.poll("p", full=True)
        turn = await live.end_turn("p", "op-1")
        tree = await live.tree("p")
        await live.close()
        return turn, sorted((file.path, file.kind) for file in tree.files)

    turn, files = run(scenario())
    assert files == [("a.md", "text"), ("checkpoint.bin", "unsupported")]
    assert {row["path"]: row["kind"] for row in turn["files"]} == {
        "a.md": "write",
        "checkpoint.bin": "unsupported",
    }
    with store.connection() as conn:
        assert conn.execute("SELECT max(size) FROM paper_blobs").fetchone()[0] <= 64


def test_a_failed_idle_save_is_retried(paper, monkeypatch):
    live, _, root = paper
    real = folder_write
    calls = [0]

    def flaky(*args, **kwargs):
        calls[0] += 1
        if calls[0] == 2:
            raise OSError("transient")
        return real(*args, **kwargs)

    monkeypatch.setattr("rcp.paper.folder_io.write", flaky)
    monkeypatch.setattr("rcp.limits.PAPER_SAVE_IDLE_SECONDS", 0.0)

    async def scenario():
        file_id = await create(live, "a.md", "hello")
        client = Client(live, file_id)
        await client.join()
        await client.type(5, " world")
        await live.tick()
        await live.tick()
        saved = (root / "p/a.md").read_text()
        await live.close()
        return saved

    assert run(scenario()) == "hello world"


def test_a_delete_that_displaced_foreign_bytes_lists_them_as_a_clash(paper, monkeypatch):
    live, _, root = paper
    from rcp.paper import folder_io

    real = folder_io.delete

    def racing(folder_root, path, expect_sha, **settle):
        (folder_root / path).write_text("agent wrote this")
        return real(folder_root, path, expect_sha, **settle)

    async def scenario():
        file_id = await create(live, "a.md", "base")
        await live.begin_turn("p", "op-1")
        monkeypatch.setattr("rcp.paper.folder_io.delete", racing)
        await live.apply("p", [PaperOp(kind="delete", file_id=file_id)], "h")
        turn = await live.end_turn("p", "op-1")
        await live.close()
        return turn

    (row,) = run(scenario())["files"]
    assert (row["path"], row["clash"], row["coach_sha"]) == (
        "a.md",
        True,
        digest(b"agent wrote this"),
    )


def test_a_multi_file_write_that_keeps_conflicting_reports_what_it_wrote(paper, monkeypatch):
    from rcp.paper.models import PaperConflict

    live, _, _root = paper
    real = live._put

    async def contested(project, path, data, room):
        if path == "b.md":
            raise PaperConflict("Paper file keeps changing under the editor")
        return await real(project, path, data, room)

    async def scenario():
        await create(live, "b.md", "base")
        monkeypatch.setattr(live, "_put", contested)
        ops = [PaperOp(kind="write", path=name, data=b"new") for name in ("a.md", "b.md")]
        with pytest.raises(PaperWriteFailed) as failure:
            await live.apply("p", ops, "h")
        await live.close()
        return failure.value.written

    assert run(scenario()) == ["a.md"]
