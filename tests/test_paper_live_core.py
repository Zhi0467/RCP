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
