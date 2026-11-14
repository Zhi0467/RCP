"""Loop-owned Paper editor over each project's Paper folder; the files are the Paper.

A room holds one open text file's Yjs buffer. A save writes the folder, then the
buffer cache, then the ack, so an ack means "on disk". While a file is open or a
Paper turn runs, a poll reads foreign writes and merges each into an open buffer,
the buffer winning overlaps. Multi-file writes go through the same save, their
result winning overlaps. Public async methods marshal callers onto the owner loop.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeVar

from pycrdt import Doc, Text, create_update_message, get_update, handle_sync_message, merge_updates

from rcp import limits
from rcp.paper.folder import PaperFolder
from rcp.paper.folder_io import Entry
from rcp.paper.git_merge import text_merge
from rcp.paper.html_preview import (
    PaperHTMLContent,
    PaperHTMLPreview,
    read_html_content,
    read_html_preview,
)
from rcp.paper.models import (
    PaperConflict,
    PaperEntry,
    PaperFile,
    PaperLimit,
    PaperOp,
    PaperPaused,
    PaperPrecondition,
    PaperRateLimit,
    PaperSnapshot,
    PaperSource,
    PaperTree,
    PaperTreeFile,
    PaperWriteFailed,
    Receipt,
    digest,
    file_format,
    paper_file_kind,
    paper_path,
)
from rcp.paper.persistence import PaperSnapshotReader, collect_bodies, save_version
from rcp.storage import AppStore

_LOG = logging.getLogger(__name__)
_Result = TypeVar("_Result")
Kind = Literal["text", "binary", "unsupported"]


def _owned(method):
    @functools.wraps(method)
    async def invoke(self, *args, **kwargs):
        current = asyncio.get_running_loop()
        if self.loop is None:
            self.loop = current
        if current is not self.loop:
            future = asyncio.run_coroutine_threadsafe(method(self, *args, **kwargs), self.loop)
            return await asyncio.wrap_future(future)
        return await method(self, *args, **kwargs)

    return invoke


def _doc(state: bytes | None = None, text: str | None = None) -> Doc:
    doc = Doc({"text": Text()}, skip_gc=False)
    if state is not None:
        doc.apply_update(state)
    if text:
        doc["text"].insert(0, text)
    return doc


def _compact_state(encoded: bytes) -> tuple[Doc, bytes]:
    doc = _doc(encoded)
    pending = get_update(encoded, doc.get_state())
    return doc, merge_updates(doc.get_update(), pending)


def _ack(vector: bytes) -> bytes:
    return bytes([100]) + vector


def _frame(message: bytes) -> tuple[int, bytes]:
    """Reject truncation/trailing bytes before passing untrusted data to pycrdt."""
    if len(message) < 3 or message[0] != 0 or message[1] not in (0, 1, 2):
        raise ValueError("Invalid Y sync frame")
    value = 0
    shift = 0
    index = 2
    while index < len(message) and shift <= 28:
        byte = message[index]
        index += 1
        value |= (byte & 127) << shift
        if byte < 128:
            if len(message) - index != value:
                raise ValueError("Truncated Y sync frame")
            return message[1], message[index:]
        shift += 7
    raise ValueError("Invalid Y sync length")


def text_replacement(old: str, new: str) -> tuple[int, int, str] | None:
    """The one changed span, so unchanged CRDT characters (and peer positions) survive."""
    if old == new:
        return None
    start = 0
    while start < min(len(old), len(new)) and old[start] == new[start]:
        start += 1
    end, new_end = len(old), len(new)
    while end > start and new_end > start and old[end - 1] == new[new_end - 1]:
        end -= 1
        new_end -= 1
    return start, end, new[start:new_end]


def _replace_text(doc: Doc, new: str) -> None:
    old = str(doc["text"])
    change = text_replacement(old, new)
    if change is None:
        return
    start, end, text = change
    begin, finish = len(old[:start].encode()), len(old[:end].encode())
    with doc.transaction():
        del doc["text"][begin:finish]
        doc["text"].insert(begin, text)


def merge_file(
    path: str, base: bytes | None, ours: bytes, theirs: bytes, *, theirs_win: bool
) -> tuple[bytes, bool]:
    """Three-way merge of one file; True when an overlap had a loser."""
    if ours == theirs or theirs == base:
        return ours, False
    if ours == base:
        return theirs, False
    texts = [data for data in (base, ours, theirs) if data is not None]
    if all(paper_file_kind(path, data) == "text" for data in texts):
        merged, other = text_merge(base or b"", ours, theirs)
        if other is None:
            return merged, False
        return (other if theirs_win else merged), True
    return (theirs if theirs_win else ours), True


def _stat(entry: Entry) -> tuple[int, int, int]:
    return entry.size, entry.mtime_ns, entry.inode


def _kind(path: str, data: bytes) -> Literal["text", "binary"]:
    return paper_file_kind(path, data)


@dataclass
class Known:
    """The editor's last-known state of one path on disk."""

    file_id: str
    kind: Kind
    sha: str
    size: int
    stat: tuple[int, int, int] | None = None


@dataclass
class Room:
    doc: Doc
    encoded: bytes
    path: str
    base: str
    saved: bytes
    subscribers: set[asyncio.Queue[bytes]] = field(default_factory=set)
    awareness: dict[int, tuple[int, str]] = field(default_factory=dict)
    edited_at: float | None = None

    @property
    def text(self) -> str:
        return str(self.doc["text"])

    def dirty(self) -> bool:
        return self.text != self.base or self.saved != self.doc.get_state()


@dataclass
class _Turn:
    operation_id: str
    version_id: str


@dataclass
class _Project:
    folder: PaperFolder
    known: dict[str, Known]
    revision: int
    turn: _Turn | None
    unstable: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    written: dict[str, bytes] = field(default_factory=dict)
    polled_at: float = 0.0
    changed_at: float = 0.0
    burst_at: float | None = None

    def path_of(self, file_id: str) -> str:
        for path, known in self.known.items():
            if known.file_id == file_id and known.kind != "unsupported":
                return path
        raise KeyError(file_id)


@dataclass
class _Read:
    entries: dict[str, tuple[int, int, int]]
    unsupported: set[str]
    bodies: dict[str, bytes]


class PaperLive:
    def __init__(
        self,
        store: AppStore,
        folder_for: Callable[[str], PaperFolder],
        prepare: Callable[[str], object] = lambda _project: None,
    ):
        self.store = store
        self.folder_for = folder_for
        self.prepare = prepare
        self.loop: asyncio.AbstractEventLoop | None = None
        self.rooms: dict[tuple[str, str], Room] = {}
        self.projects: dict[str, _Project] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._paused: set[str] = set()
        self._disposed: set[str] = set()
        self._fenced = False
        self._timer: asyncio.Task | None = None
        self._background: set[asyncio.Task] = set()
        self._last_collection = time.monotonic()
        self._rates: dict[str, list[float]] = {}

    async def open_projects(self) -> set[str]:
        return {project for (project, _file_id), room in self.rooms.items() if room.subscribers}

    def run_sync(self, action: Coroutine[Any, Any, _Result]) -> _Result:
        """Marshal a control worker to this room owner without blocking its event loop."""
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if self.loop is None or not self.loop.is_running() or current is self.loop:
            action.close()
            raise RuntimeError("Synchronous Paper access requires a worker and a running owner")
        return asyncio.run_coroutine_threadsafe(action, self.loop).result()

    def _sql(self, sql: str, parameters=()) -> list[Any]:
        with self.store.connection() as conn:
            return conn.execute(sql, parameters).fetchall()

    def _lock(self, project: str) -> asyncio.Lock:
        return self._locks.setdefault(project, asyncio.Lock())

    def _writable(self, project: str) -> None:
        if self._fenced or project in self._paused:
            raise PaperPaused("Paper is paused")
        self._disposed.discard(project)

    def _project_rooms(self, project: str) -> list[Room]:
        return [room for (owner, _), room in self.rooms.items() if owner == project]

    def _room_at(self, project: str, path: str) -> Room | None:
        state = self.projects[project]
        known = state.known.get(path)
        return self.rooms.get((project, known.file_id)) if known else None

    async def start(self) -> None:
        if self.loop is not None and self.loop.is_closed():
            self.__init__(self.store, self.folder_for, self.prepare)
        await self._start()

    @_owned
    async def _start(self) -> None:
        self._fenced = False
        if self._timer is None:
            for (project,) in await asyncio.to_thread(self._sql, "SELECT project_id FROM projects"):
                try:
                    await asyncio.to_thread(self.store.require_project_accepts_new_work, project)
                except ValueError:
                    self._disposed.add(project)
            self._timer = asyncio.create_task(self._tick())

    def fence(self) -> None:
        self._fenced = True

    @_owned
    async def close(self) -> None:
        self.fence()
        if self._timer:
            self._timer.cancel()
            await asyncio.gather(self._timer, return_exceptions=True)
            self._timer = None
        for project in list(self.projects):
            async with self._lock(project):
                await self._flush_quietly(project)
        await asyncio.gather(*self._background, return_exceptions=True)
        for room in self.rooms.values():
            self._broadcast(room, b"")
        self.rooms.clear()
        self.projects.clear()

    async def _flush_quietly(self, project: str) -> None:
        try:
            await self._flush(project)
        except Exception:
            _LOG.exception("Paper buffers of project %s could not be saved", project)

    @_owned
    async def pause(self, project_id: str) -> None:
        self._paused.add(project_id)
        async with self._lock(project_id):
            pass

    @_owned
    async def resume(self, project_id: str) -> None:
        async with self._lock(project_id):
            try:
                await asyncio.to_thread(self.store.require_project_accepts_new_work, project_id)
            except ValueError:
                return
            self._disposed.discard(project_id)
            self._paused.discard(project_id)

    @_owned
    async def drain(self, project_id: str) -> None:
        async with self._lock(project_id):
            pass
        await asyncio.gather(*self._background, return_exceptions=True)

    @_owned
    async def dispose(self, project_id: str) -> None:
        await self.pause(project_id)
        async with self._lock(project_id):
            if project_id in self.projects:
                await self._flush_quietly(project_id)
            for key in [key for key in self.rooms if key[0] == project_id]:
                self._broadcast(self.rooms.pop(key), b"")
            self.projects.pop(project_id, None)
            self._disposed.add(project_id)
            self._paused.discard(project_id)
        await self.drain(project_id)

    async def _ensure(self, project: str) -> _Project:
        state = self.projects.get(project)
        if state is not None:
            return state
        if self._fenced:
            raise PaperPaused("Paper is shutting down")
        if project in self._disposed:
            raise PaperPaused("Paper is paused")
        state = await asyncio.to_thread(self._initialize, project)
        self.projects[project] = state
        return state

    def _initialize(self, project: str) -> _Project:
        self.prepare(project)
        folder = self.folder_for(project)
        with self.store.connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO paper_state(project_id, revision) VALUES (?, 0)", (project,)
            )
            row = conn.execute(
                "SELECT revision, turn_operation_id, turn_version_id FROM paper_state "
                "WHERE project_id=?",
                (project,),
            ).fetchone()
            identities = {
                path: (file_id, kind)
                for file_id, path, kind in conn.execute(
                    "SELECT file_id, path, kind FROM paper_files "
                    "WHERE project_id=? AND deleted_at IS NULL",
                    (project,),
                )
            }
            cached = dict(
                conn.execute(
                    "SELECT file_id, text_sha256 FROM paper_doc_states "
                    "WHERE project_id=? AND text_sha256 IS NOT NULL",
                    (project,),
                ).fetchall()
            )
        turn = _Turn(row[1], row[2]) if row[1] else None
        state = _Project(folder, {}, int(row[0]), turn)
        read = self._read_folder(state, full=True)
        vanished = {
            path: file_id
            for path, (file_id, _kind) in identities.items()
            if path not in read.bodies
        }
        by_sha = {cached[file_id]: path for path, file_id in vanished.items() if file_id in cached}
        changed = False
        now = self.store.now()
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for path, body in sorted(read.bodies.items()):
                sha = digest(body)
                kind = _kind(path, body)
                identity = identities.get(path)
                if identity is None and sha in by_sha:
                    identity = (vanished.pop(by_sha.pop(sha)), kind)
                if identity is None:
                    identity = (str(uuid.uuid4()), kind)
                if identities.get(path) != (identity[0], kind):
                    changed = True
                    self._identity(conn, project, identity[0], path, kind, now)
                state.known[path] = Known(identity[0], kind, sha, len(body), read.entries[path])
            for file_id in vanished.values():
                changed = True
                conn.execute(
                    "UPDATE paper_files SET deleted_at=? WHERE project_id=? AND file_id=?",
                    (now, project, file_id),
                )
            if changed:
                state.revision = self._bump(conn, project)
        for path in read.unsupported:
            state.known[path] = Known(f"unsupported:{path}", "unsupported", "", 0)
        return state

    @staticmethod
    def _identity(conn, project: str, file_id: str, path: str, kind: str, now: str) -> None:
        conn.execute(
            "UPDATE paper_files SET deleted_at=? WHERE project_id=? AND path=? "
            "AND file_id<>? AND deleted_at IS NULL",
            (now, project, path, file_id),
        )
        conn.execute(
            "INSERT INTO paper_files(project_id,file_id,path,kind,created_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(project_id,file_id) DO UPDATE SET path=excluded.path,"
            "kind=excluded.kind,deleted_at=NULL",
            (project, file_id, path, kind, now),
        )

    @staticmethod
    def _bump(conn, project: str) -> int:
        conn.execute("UPDATE paper_state SET revision=revision+1 WHERE project_id=?", (project,))
        return conn.execute(
            "SELECT revision FROM paper_state WHERE project_id=?", (project,)
        ).fetchone()[0]

    def _read_folder(self, state: _Project, *, full: bool) -> _Read:
        """Stat everything; read only stat-changed paths, stable for one poll unless `full`."""
        entries = state.folder.scan()
        stats = {entry.path: _stat(entry) for entry in entries if entry.kind == "file"}
        unsupported = {entry.path for entry in entries if entry.kind == "unsupported"}
        reads: list[str] = []
        budget = limits.PAPER_POLL_MAX_HASH_MB * 1024 * 1024
        for entry in entries:
            if entry.kind != "file":
                continue
            known = state.known.get(entry.path)
            if known is not None and known.kind != "unsupported" and known.stat == _stat(entry):
                state.unstable.pop(entry.path, None)
                continue
            if not full:
                if state.unstable.get(entry.path) != _stat(entry):
                    state.unstable[entry.path] = _stat(entry)
                    continue
                if reads and (len(reads) >= limits.PAPER_POLL_MAX_FILES or entry.size > budget):
                    continue
                budget -= entry.size
            state.unstable.pop(entry.path, None)
            reads.append(entry.path)
        bodies = {
            path: body
            for path, body in (state.folder.read(reads) if reads else {}).items()
            if body is not None
        }
        return _Read(stats, unsupported, bodies)

    async def _poll(self, project: str, *, full: bool = False) -> bool:
        """Absorb what changed on disk since the last look; True when anything did."""
        state = self.projects[project]
        state.polled_at = time.monotonic()
        read = await asyncio.to_thread(self._read_folder, state, full=full)
        changed = False
        fresh: dict[str, bytes] = {}
        for path, body in read.bodies.items():
            known = state.known.get(path)
            if known is not None and known.kind != "unsupported" and known.sha == digest(body):
                known.stat = read.entries[path]
                continue
            fresh[path] = body
        vanished = [
            path
            for path, known in state.known.items()
            if known.kind != "unsupported" and path not in read.entries
        ]
        for path in [
            path
            for path, known in state.known.items()
            if known.kind == "unsupported" and path not in read.unsupported
        ]:
            del state.known[path]
        for path in sorted(read.unsupported):
            known = state.known.get(path)
            if known is not None and known.kind == "unsupported":
                continue
            changed = True
            await self._record(project, path, "unsupported", None, None, False)
        for path in sorted(vanished):
            target = next(
                (
                    name
                    for name, body in fresh.items()
                    if name not in state.known and digest(body) == state.known[path].sha
                ),
                None,
            )
            room = self._room_at(project, path)
            clash = room is not None and await self._edited(project, room)
            if target is not None and not clash:
                await self._renamed(project, path, target, fresh.pop(target), read)
            else:
                await self._vanished(project, path, room, clash)
            changed = True
        for path, body in sorted(fresh.items()):
            await self._foreign(project, path, body, read.entries[path])
            changed = True
        for path in read.unsupported:
            if path not in state.known:
                state.known[path] = Known(f"unsupported:{path}", "unsupported", "", 0)
        if changed:
            await self._changed(project)
        return changed

    async def _edited(self, project: str, room: Room) -> bool:
        base = await self._base(project, room.path, room)
        return room.text.encode() != base

    async def _renamed(self, project: str, src: str, dst: str, body: bytes, read: _Read) -> None:
        state = self.projects[project]
        known = state.known.pop(src)
        known.stat = read.entries[dst]
        state.known[dst] = known
        room = self.rooms.get((project, known.file_id))
        if room is not None:
            room.path = dst
        await asyncio.to_thread(self._persist_identity, project, known.file_id, dst, known.kind)
        await self._record(project, dst, "rename", body, body, False)

    async def _vanished(self, project: str, path: str, room: Room | None, clash: bool) -> None:
        state = self.projects[project]
        known = state.known[path]
        if clash and room is not None:
            base = await self._base(project, path, room)
            known.sha, known.stat = "", None
            await self._put(project, path, room.text.encode(), room)
            room.base = room.text
            await self._cache_and_ack(project, room)
            await self._record(project, path, "delete", base, None, True)
            return
        del state.known[path]
        state.written.pop(path, None)
        if room is not None:
            self._broadcast(self.rooms.pop((project, known.file_id)), b"")
        await asyncio.to_thread(self._forget, project, known.file_id)
        await self._record(project, path, "delete", None, None, False)

    async def _foreign(
        self, project: str, path: str, theirs: bytes, stat: tuple[int, int, int] | None
    ) -> None:
        """A write RCP did not make: an open buffer or a turn-edited file keeps its edits."""
        state = self.projects[project]
        known = state.known.get(path)
        room = self._room_at(project, path)
        ours = room.text.encode() if room is not None else state.written.get(path)
        if known is not None and ours is not None and digest(ours) != known.sha and room is None:
            ours = None
        base = await self._base(project, path, room)
        if ours is None:
            result, clash = theirs, False
        else:
            result, clash = await asyncio.to_thread(
                merge_file, path, base, ours, theirs, theirs_win=False
            )
        kind = _kind(path, theirs)
        if known is None:
            known = Known(str(uuid.uuid4()), kind, digest(theirs), len(theirs), stat)
            state.known[path] = known
            await asyncio.to_thread(self._persist_identity, project, known.file_id, path, kind)
        else:
            known.sha, known.size, known.stat = digest(theirs), len(theirs), stat
            if known.kind != kind:
                known.kind = kind
                await asyncio.to_thread(self._persist_identity, project, known.file_id, path, kind)
        await self._record(project, path, "write", base, theirs, clash)
        if room is not None and _kind(path, result) != "text":
            self._broadcast(self.rooms.pop((project, known.file_id)), b"")
            room = None
        if room is not None:
            self._set_text(room, result.decode())
        if result != theirs:
            await self._put(project, path, result, room)
        if room is not None:
            room.base = room.text
            await self._cache_and_ack(project, room)

    async def _base(self, project: str, path: str, room: Room | None) -> bytes | None:
        state = self.projects[project]
        if state.turn is not None:
            return await asyncio.to_thread(self._turn_base, project, state.turn, path)
        return room.base.encode() if room is not None else None

    def _turn_base(self, project: str, turn: _Turn, path: str) -> bytes | None:
        """The agent's previous write to `path` in this turn, else the turn-start version."""
        with self.store.connection() as conn:
            row = conn.execute(
                "SELECT b.bytes FROM paper_turn_files f JOIN paper_blobs b ON b.sha256=f.coach_sha "
                "WHERE f.project_id=? AND f.operation_id=? AND f.path=? "
                "ORDER BY f.rowid DESC LIMIT 1",
                (project, turn.operation_id, path),
            ).fetchone()
            if row is not None:
                return bytes(row[0])
            return PaperSnapshotReader(conn).version_file(project, turn.version_id, path)

    async def _record(
        self,
        project: str,
        path: str,
        kind: str,
        base: bytes | None,
        coach: bytes | None,
        clash: bool,
    ) -> None:
        turn = self.projects[project].turn
        await asyncio.to_thread(
            self._insert_turn_file,
            project,
            turn.operation_id if turn else None,
            path,
            kind if turn else "outside_turn",
            base,
            coach,
            clash,
        )

    def _insert_turn_file(self, project, operation_id, path, kind, base, coach, clash) -> None:
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for data in (base, coach):
                if data is not None:
                    conn.execute(
                        "INSERT OR IGNORE INTO paper_blobs VALUES(?,?,?)",
                        (digest(data), data, len(data)),
                    )
            conn.execute(
                "INSERT INTO paper_turn_files(project_id,operation_id,path,base_sha,coach_sha,"
                "clash,kind,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    project,
                    operation_id,
                    path,
                    digest(base) if base is not None else None,
                    digest(coach) if coach is not None else None,
                    int(clash),
                    kind,
                    self.store.now(),
                ),
            )

    def _persist_identity(self, project: str, file_id: str, path: str, kind: str) -> None:
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._identity(conn, project, file_id, path, kind, self.store.now())

    def _forget(self, project: str, file_id: str) -> None:
        with self.store.connection() as conn:
            conn.execute(
                "UPDATE paper_files SET deleted_at=? WHERE project_id=? AND file_id=?",
                (self.store.now(), project, file_id),
            )

    async def _changed(self, project: str) -> int:
        state = self.projects[project]
        state.revision = await asyncio.to_thread(self._persist_bump, project)
        state.changed_at = time.monotonic()
        state.burst_at = state.burst_at or state.changed_at
        return state.revision

    def _persist_bump(self, project: str) -> int:
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._bump(conn, project)

    async def _put(self, project: str, path: str, data: bytes, room: Room | None) -> None:
        """Write one file; bytes a racing foreign write displaced are merged, never lost."""
        state = self.projects[project]
        for _attempt in range(limits.PAPER_SAVE_RETRIES):
            known = state.known.get(path)
            expect = known.sha if known is not None and known.sha else None
            result = await asyncio.to_thread(state.folder.write, path, data, expect)
            if known is not None:
                known.sha, known.size, known.stat = digest(data), len(data), None
            if state.turn is not None:
                state.written[path] = data
            if not result.conflict or result.displaced is None:
                return
            theirs = result.displaced
            base = await self._base(project, path, room)
            merged, clash = await asyncio.to_thread(
                merge_file, path, base, data, theirs, theirs_win=False
            )
            await self._record(project, path, "write", base, theirs, clash)
            if merged == data:
                return
            data = merged
            if room is not None:
                self._set_text(room, merged.decode())
        raise PaperConflict("Paper file keeps changing under the editor")

    def _set_text(self, room: Room, text: str) -> None:
        before = room.doc.get_state()
        _replace_text(room.doc, text)
        update = room.doc.get_update(before)
        if update and before != room.doc.get_state():
            room.encoded = merge_updates(room.encoded, update)
            self._broadcast(room, create_update_message(update))

    async def _save(self, project: str, room: Room) -> None:
        room.edited_at = None
        if room.text != room.base:
            data = room.text.encode()
            if b"\0" in data:
                raise ValueError("Paper text cannot contain NUL")
            if len(data) > limits.PAPER_MAX_DOC_BYTES:
                raise PaperLimit("Paper document is too large")
            await self._put(project, room.path, data, room)
            room.base = room.text
            await self._changed(project)
        await self._cache_and_ack(project, room)

    async def _cache_and_ack(self, project: str, room: Room) -> None:
        state = self.projects[project]
        known = state.known[room.path]
        vector = room.doc.get_state()
        _, room.encoded = await asyncio.to_thread(_compact_state, room.encoded)
        await asyncio.to_thread(
            self._cache,
            project,
            known.file_id,
            room.encoded,
            state.revision,
            digest(room.base.encode()),
        )
        room.saved = vector
        self._broadcast(room, _ack(vector))

    def _cache(self, project: str, file_id: str, encoded: bytes, revision: int, sha: str) -> None:
        with self.store.connection() as conn:
            conn.execute(
                "INSERT INTO paper_doc_states(project_id,file_id,state,through_seq,"
                "through_revision,text_sha256) VALUES(?,?,?,0,?,?) "
                "ON CONFLICT(project_id,file_id) DO UPDATE SET state=excluded.state,"
                "through_revision=excluded.through_revision,text_sha256=excluded.text_sha256",
                (project, file_id, encoded, revision, sha),
            )

    async def _flush(self, project: str) -> None:
        for room in self._project_rooms(project):
            if room.dirty():
                await self._save(project, room)

    async def _room(self, project: str, file_id: str) -> Room:
        key = (project, file_id)
        if key in self.rooms:
            return self.rooms[key]
        if sum(owner == project for owner, _ in self.rooms) >= limits.PAPER_MAX_ROOMS_PER_PROJECT:
            for old in list(self.rooms):
                room = self.rooms[old]
                if old[0] == project and not room.subscribers and not room.dirty():
                    del self.rooms[old]
                    break
            else:
                raise PaperLimit("Too many open Paper rooms")
        await self._poll(project, full=True)
        state = self.projects[project]
        path = state.path_of(file_id)
        if state.known[path].kind != "text":
            raise KeyError(file_id)
        room = await asyncio.to_thread(self._load_room, project, file_id, path, state.revision)
        self.rooms[key] = room
        return room

    def _load_room(self, project: str, file_id: str, path: str, revision: int) -> Room:
        """The cached buffer when it equals disk; else the cache moved to the disk text."""
        state = self.projects[project]
        data = state.folder.read([path]).get(path)
        if data is None:
            raise KeyError(file_id)
        text = data.decode()
        sha = digest(data)
        with self.store.connection() as conn:
            row = conn.execute(
                "SELECT state, text_sha256 FROM paper_doc_states WHERE project_id=? AND file_id=?",
                (project, file_id),
            ).fetchone()
        if row is not None and row[1] == sha:
            doc, encoded = _compact_state(row[0])
        else:
            doc = _doc(row[0]) if row is not None else _doc()
            _replace_text(doc, text)
            doc, encoded = _compact_state(doc.get_update())
            self._cache(project, file_id, encoded, revision, sha)
        return Room(doc, encoded, path, str(doc["text"]), doc.get_state())

    @_owned
    async def connect(
        self, project_id: str, file_id: str, subscriber: asyncio.Queue[bytes] | None = None
    ) -> Room:
        async with self._lock(project_id):
            self._writable(project_id)
            await self._ensure(project_id)
            room = await self._room(project_id, file_id)
            if subscriber is not None:
                room.subscribers.add(subscriber)
            return room

    @_owned
    async def leave(self, project_id: str, file_id: str, subscriber: asyncio.Queue[bytes]) -> None:
        """A room's last viewer leaving saves its buffer."""
        async with self._lock(project_id):
            room = self.rooms.get((project_id, file_id))
            if room is None:
                return
            room.subscribers.discard(subscriber)
            if not room.subscribers and room.dirty() and project_id in self.projects:
                await self._save(project_id, room)

    def file_exists(self, project_id: str, file_id: str) -> bool:
        state = self.projects.get(project_id)
        if state is not None:
            return any(known.file_id == file_id for known in state.known.values())
        with self.store.connection() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM paper_files WHERE project_id=? AND file_id=? "
                    "AND deleted_at IS NULL",
                    (project_id, file_id),
                ).fetchone()
                is not None
            )

    def _rate(self, project: str) -> None:
        now = time.monotonic()
        rate = [moment for moment in self._rates.get(project, []) if moment >= now - 1]
        if len(rate) >= limits.PAPER_PROJECT_UPDATES_PER_SECOND:
            self._rates[project] = rate
            raise PaperRateLimit("Paper project update rate exceeded")
        rate.append(now)
        self._rates[project] = rate

    @_owned
    async def receive(
        self,
        project_id: str,
        file_id: str,
        message: bytes,
        actor: str,
        sender: asyncio.Queue[bytes] | None = None,
    ) -> list[bytes]:
        """Return the sender's replies. An update is acked once the buffer is on disk."""
        async with self._lock(project_id):
            self._writable(project_id)
            await self._ensure(project_id)
            if not message or len(message) > limits.PAPER_MAX_MESSAGE_BYTES:
                raise PaperLimit("Paper message is too large")
            room = await self._room(project_id, file_id)
            if message[0] == 1:
                self._rate(project_id)
                self._broadcast(room, message)
                return []
            kind, update = _frame(message)
            if kind == 0:
                reply = handle_sync_message(message[1:], room.doc)
                return [reply, _ack(room.saved)] if reply else [_ack(room.saved)]
            encoded = merge_updates(room.encoded, update)
            if encoded == room.encoded:
                return [_ack(room.saved)]
            candidate, encoded = await asyncio.to_thread(_compact_state, encoded)
            if len(encoded) > limits.PAPER_MAX_MESSAGE_BYTES:
                raise PaperLimit("Paper CRDT state is too large")
            body = str(candidate["text"]).encode()
            if b"\0" in body:
                raise ValueError("Paper text cannot contain NUL")
            if len(body) > limits.PAPER_MAX_DOC_BYTES:
                raise PaperLimit("Paper document is too large")
            self._rate(project_id)
            room.doc, room.encoded = candidate, encoded
            room.edited_at = time.monotonic()
            self._broadcast(room, create_update_message(update), skip=sender)
            return []

    @staticmethod
    def _broadcast(room: Room, message: bytes, skip: asyncio.Queue[bytes] | None = None) -> None:
        for queue in tuple(room.subscribers - {skip}):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(b"")

    @_owned
    async def flush(self, project_id: str) -> None:
        async with self._lock(project_id):
            await self._ensure(project_id)
            await self._flush(project_id)

    @_owned
    async def poll(self, project_id: str, *, full: bool = False) -> bool:
        async with self._lock(project_id):
            await self._ensure(project_id)
            return await self._poll(project_id, full=full)

    async def _current(self, project: str) -> PaperSnapshot:
        """Flush, absorb disk, then read every file: the Paper as it is now."""
        await self._flush(project)
        await self._poll(project, full=True)
        state = self.projects[project]
        paths = sorted(path for path, known in state.known.items() if known.kind != "unsupported")
        bodies = await asyncio.to_thread(state.folder.read, paths)
        files: list[PaperFile] = []
        blobs: dict[str, bytes] = {}
        for path in paths:
            body = bodies.get(path)
            if body is None:
                continue
            known = state.known[path]
            if _kind(path, body) == "text":
                files.append(
                    PaperFile(file_id=known.file_id, path=path, kind="text", text=body.decode())
                )
            else:
                sha = digest(body)
                blobs[sha] = body
                files.append(
                    PaperFile(file_id=known.file_id, path=path, kind="binary", blob_sha256=sha)
                )
        snapshot = PaperSnapshot(revision=state.revision, files=files)
        snapshot._blobs = blobs
        return snapshot

    def _version(self, project: str, version_id: str) -> PaperSnapshot:
        with self.store.connection() as conn:
            return PaperSnapshotReader(conn).snapshot(project, version_id)

    @_owned
    async def snapshot(self, project_id: str, version_id: str | None = None) -> PaperSnapshot:
        if version_id is not None:
            return await asyncio.to_thread(self._version, project_id, version_id)
        async with self._lock(project_id):
            await self._ensure(project_id)
            return await self._current(project_id)

    @_owned
    async def tree(self, project_id: str) -> PaperTree:
        async with self._lock(project_id):
            state = await self._ensure(project_id)
            return PaperTree(
                revision=state.revision,
                files=[
                    PaperTreeFile(
                        file_id=known.file_id,
                        path=path,
                        kind=known.kind,
                        format=file_format(
                            path, "binary" if known.kind == "unsupported" else known.kind
                        ),
                        size=known.size,
                        sha256=known.sha,
                    )
                    for path, known in sorted(state.known.items())
                ],
            )

    @_owned
    async def raw_file(
        self, project_id: str, file_id: str, version_id: str | None = None
    ) -> tuple[PaperEntry, bytes]:
        source = await self.source(project_id, version_id)
        entry = next((entry for entry in source.entries if entry.file_id == file_id), None)
        if entry is None:
            raise KeyError(file_id)
        return entry, await asyncio.to_thread(source.read, entry)

    @_owned
    async def source(self, project_id: str, version_id: str | None = None) -> PaperSource:
        """Entries of the current Paper or a version, reading bodies only on demand."""
        if version_id is not None:
            return await asyncio.to_thread(self._version_source, project_id, version_id)
        async with self._lock(project_id):
            state = await self._ensure(project_id)
            await self._flush(project_id)
            entries = [
                PaperEntry(known.file_id, path, known.kind, known.sha, known.size)
                for path, known in sorted(state.known.items())
                if known.kind != "unsupported"
            ]
            folder = state.folder

            def read(entry: PaperEntry) -> bytes:
                data = folder.read([entry.path]).get(entry.path)
                if data is None:
                    raise KeyError(entry.file_id)
                return data

            return PaperSource(state.revision, entries, read)

    def _version_source(self, project_id: str, version_id: str) -> PaperSource:
        with self.store.connection() as conn:
            revision, entries = PaperSnapshotReader(conn).entries(project_id, version_id)

        def read(entry: PaperEntry) -> bytes:
            with self.store.connection() as conn:
                return PaperSnapshotReader(conn).raw_file(project_id, entry.file_id, version_id)[1]

        return PaperSource(revision, entries, read)

    @_owned
    async def html_preview_state(
        self, project_id: str, file_id: str, version_id: str | None = None
    ) -> PaperHTMLPreview:
        source = await self.source(project_id, version_id)
        return await asyncio.to_thread(read_html_preview, source, file_id)

    @_owned
    async def html_preview(
        self, project_id: str, file_id: str, version_id: str | None = None
    ) -> PaperHTMLContent:
        source = await self.source(project_id, version_id)
        return await asyncio.to_thread(read_html_content, source, file_id)

    def materialize(self, snapshot: PaperSnapshot, dest: Path) -> None:
        PaperSnapshotReader.materialize(snapshot, dest)

    @_owned
    async def text_at(self, project_id: str, file_id: str, version_id: str | None = None) -> str:
        entry, data = await self.raw_file(project_id, file_id, version_id)
        if entry.kind != "text":
            raise KeyError(file_id)
        return data.decode()

    def _plan(self, state: _Project, ops: list[PaperOp]) -> list[tuple[str, int, PaperOp]]:
        """Validate the whole write against limits first; then it runs in path order."""
        tree = {
            path: known.size for path, known in state.known.items() if known.kind != "unsupported"
        }
        ids = {
            known.file_id: path
            for path, known in state.known.items()
            if known.kind != "unsupported"
        }
        steps: list[tuple[str, int, PaperOp]] = []
        for op in ops:
            if op.kind == "write":
                path = paper_path(op.path or "")
                data = op.data or b""
                if op.exclusive and path in tree:
                    raise PaperPrecondition("Paper path is occupied")
                limit = (
                    limits.PAPER_MAX_DOC_BYTES
                    if _kind(path, data) == "text"
                    else limits.PAPER_MAX_UPLOAD_BYTES
                )
                if len(data) > limit:
                    raise PaperLimit("Paper file is too large")
                tree[path] = len(data)
                steps.append((path, 2, op))
                continue
            source = ids.get(op.file_id or "")
            if source is None:
                if op.kind == "rename" and op.path in tree:
                    continue
                if op.kind == "delete":
                    continue
                raise PaperPrecondition("Paper file no longer exists")
            if op.kind == "delete":
                del tree[source]
                ids.pop(op.file_id or "")
                steps.append((source, 0, op))
                continue
            path = paper_path(op.path or "")
            if path == source:
                continue
            if path in tree:
                raise PaperPrecondition("Paper path is occupied")
            tree[path] = tree.pop(source)
            ids[op.file_id or ""] = path
            steps.append((path, 1, op))
        if len(tree) > limits.PAPER_MAX_FILES:
            raise PaperLimit("Paper file limit reached")
        for path in tree:
            parts = path.split("/")
            if any("/".join(parts[:index]) in tree for index in range(1, len(parts))):
                raise PaperPrecondition("Paper file blocks a directory")
        if sum(tree.values()) > limits.PAPER_MAX_TREE_BYTES:
            raise PaperLimit("Paper tree is too large")
        return sorted(steps, key=lambda step: (step[0], step[1]))

    @_owned
    async def apply(
        self, project_id: str, ops: list[PaperOp], actor: str, *, checkpoint: bool = False
    ) -> Receipt:
        """A multi-file write in path order; an I/O failure reports what was written."""
        if not ops:
            raise ValueError("Paper operations cannot be empty")
        async with self._lock(project_id):
            self._writable(project_id)
            state = await self._ensure(project_id)
            await self._flush(project_id)
            await self._poll(project_id, full=True)
            steps = self._plan(state, ops)
            if checkpoint:
                await self._checkpoint(project_id)
            written: list[str] = []
            file_ids: list[str] = []
            for path, _rank, op in steps:
                try:
                    file_id = await self._perform(project_id, path, op)
                except (OSError, ValueError) as exc:
                    if isinstance(exc, PaperConflict) and not isinstance(exc, PaperWriteFailed):
                        raise
                    raise PaperWriteFailed(written, path) from exc
                written.append(path)
                if file_id is not None:
                    file_ids.append(file_id)
            return Receipt(revision=state.revision, file_ids=file_ids)

    async def _perform(self, project: str, path: str, op: PaperOp) -> str | None:
        state = self.projects[project]
        if op.kind == "delete":
            known = state.known.get(path)
            if known is None:
                return None
            room = self.rooms.pop((project, known.file_id), None)
            try:
                await asyncio.to_thread(state.folder.delete, path, known.sha or None)
                del state.known[path]
                state.written.pop(path, None)
                await asyncio.to_thread(self._forget, project, known.file_id)
            finally:
                if room is not None:
                    self._broadcast(room, b"")
            await self._changed(project)
            return known.file_id
        if op.kind == "rename":
            source = state.path_of(op.file_id or "")
            await asyncio.to_thread(state.folder.rename, source, path)
            known = state.known.pop(source)
            known.stat = None
            state.known[path] = known
            if source in state.written:
                state.written[path] = state.written.pop(source)
            room = self.rooms.get((project, known.file_id))
            if room is not None:
                room.path = path
            await asyncio.to_thread(
                self._persist_identity, project, known.file_id, path, known.kind
            )
            await self._changed(project)
            return known.file_id
        data = op.data or b""
        known = state.known.get(path)
        if known is None:
            known = Known(str(uuid.uuid4()), _kind(path, data), "", len(data))
            state.known[path] = known
            await self._put(project, path, data, None)
            await asyncio.to_thread(
                self._persist_identity, project, known.file_id, path, known.kind
            )
            await self._changed(project)
            return known.file_id
        room = self.rooms.get((project, known.file_id))
        if room is not None:
            ours = room.text.encode()
        else:
            ours = (await asyncio.to_thread(state.folder.read, [path])).get(path) or b""
        result, _clash = await asyncio.to_thread(
            merge_file, path, op.base, ours, data, theirs_win=True
        )
        if result == ours:
            return known.file_id
        kind = _kind(path, result)
        if room is not None and kind == "text":
            self._set_text(room, result.decode())
            await self._save(project, room)
            return known.file_id
        if room is not None:
            self._broadcast(self.rooms.pop((project, known.file_id)), b"")
        await self._put(project, path, result, None)
        if kind != known.kind:
            known.kind = kind
            await asyncio.to_thread(self._persist_identity, project, known.file_id, path, kind)
        await self._changed(project)
        return known.file_id

    async def _checkpoint(self, project: str) -> None:
        state = self.projects[project]
        latest = await asyncio.to_thread(
            self._sql,
            "SELECT COALESCE(MAX(revision), -1) FROM paper_versions WHERE project_id=?",
            (project,),
        )
        if latest[0][0] < state.revision:
            await self._save_version(project, "automatic", None)

    async def _save_version(self, project: str, actor: str, name: str | None) -> str:
        snapshot = await self._current(project)
        version_id = await asyncio.to_thread(self._insert_version, project, snapshot, actor, name)
        self.projects[project].burst_at = None
        return version_id

    def _insert_version(
        self, project: str, snapshot: PaperSnapshot, actor: str, name: str | None
    ) -> str:
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for sha, data in snapshot._blobs.items():
                conn.execute(
                    "INSERT OR IGNORE INTO paper_blobs VALUES(?,?,?)", (sha, data, len(data))
                )
            return save_version(conn, project, snapshot, actor, self.store.now(), name)

    @_owned
    async def begin_turn(self, project_id: str, operation_id: str) -> str:
        """Start a Paper turn: flush, take the before version, claim outside-turn writes."""
        async with self._lock(project_id):
            state = await self._ensure(project_id)
            if state.turn is not None:
                raise PaperConflict("Another Paper turn is running")
            version_id = await self._save_version(project_id, "automatic", None)
            await asyncio.to_thread(self._start_turn, project_id, operation_id, version_id)
            state.turn = _Turn(operation_id, version_id)
            state.written = {}
            return version_id

    def _start_turn(self, project: str, operation_id: str, version_id: str) -> None:
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE paper_state SET turn_operation_id=?, turn_version_id=? WHERE project_id=?",
                (operation_id, version_id, project),
            )
            conn.execute(
                "UPDATE paper_turn_files SET operation_id=? "
                "WHERE project_id=? AND operation_id IS NULL",
                (operation_id, project),
            )

    @_owned
    async def end_turn(self, project_id: str, operation_id: str) -> dict[str, Any]:
        """Finish a Paper turn: absorb its last writes and return its change list."""
        async with self._lock(project_id):
            state = await self._ensure(project_id)
            if state.turn is None or state.turn.operation_id != operation_id:
                raise KeyError(operation_id)
            before = state.turn.version_id
            after = await self._save_version(project_id, "automatic", None)
            await asyncio.to_thread(self._finish_turn, project_id)
            state.turn = None
            state.written = {}
        return {
            "before_version_id": before,
            "after_version_id": after,
            "files": await asyncio.to_thread(self.turn_files, project_id, operation_id),
        }

    def _finish_turn(self, project: str) -> None:
        with self.store.connection() as conn:
            conn.execute(
                "UPDATE paper_state SET turn_operation_id=NULL, turn_version_id=NULL "
                "WHERE project_id=?",
                (project,),
            )

    def turn_files(self, project_id: str, operation_id: str) -> list[dict[str, Any]]:
        """One row per path a turn's foreign writes touched, in write order."""
        files: dict[str, dict[str, Any]] = {}
        for path, kind, clash, base, coach in self._sql(
            "SELECT path, kind, clash, base_sha, coach_sha FROM paper_turn_files "
            "WHERE project_id=? AND operation_id=? ORDER BY rowid",
            (project_id, operation_id),
        ):
            row = files.setdefault(
                path,
                {"path": path, "kind": kind, "clash": False, "base_sha": base, "coach_sha": None},
            )
            row["kind"] = kind
            row["clash"] = row["clash"] or bool(clash)
            row["coach_sha"] = coach
        return list(files.values())

    @_owned
    async def history(self, project_id: str) -> list[dict[str, Any]]:
        rows = await asyncio.to_thread(
            self._sql,
            "SELECT * FROM paper_versions WHERE project_id=? ORDER BY created_at DESC,rowid DESC",
            (project_id,),
        )
        return [dict(row) | {"manifest": json.loads(row["manifest"])} for row in rows]

    @_owned
    async def version_snapshot(self, project_id: str, version_id: str) -> PaperSnapshot:
        return await asyncio.to_thread(self._version, project_id, version_id)

    @_owned
    async def create_version(self, project_id: str, name: str, actor: str) -> dict[str, Any]:
        async with self._lock(project_id):
            self._writable(project_id)
            await self._ensure(project_id)
            version_id = await self._save_version(project_id, actor, name)
        (row,) = await asyncio.to_thread(
            self._sql,
            "SELECT * FROM paper_versions WHERE project_id=? AND version_id=?",
            (project_id, version_id),
        )
        return dict(row) | {"manifest": json.loads(row["manifest"])}

    @_owned
    async def delete_version(self, project_id: str, version_id: str) -> None:
        async with self._lock(project_id):
            self._writable(project_id)
            await asyncio.to_thread(self._delete_version, project_id, version_id)

    def _delete_version(self, project_id: str, version_id: str) -> None:
        from rcp.paper.persistence import prune

        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT kind FROM paper_versions WHERE project_id=? AND version_id=?",
                (project_id, version_id),
            ).fetchone()
            if row is None:
                raise KeyError(version_id)
            if row[0] != "named":
                raise PaperConflict("Only named Paper versions can be deleted")
            conn.execute(
                "DELETE FROM paper_versions WHERE project_id=? AND version_id=?",
                (project_id, version_id),
            )
            prune(conn, project_id, refuse=False)

    @_owned
    async def restore(
        self, project_id: str, version_id: str, file_ids: list[str], actor: str
    ) -> Receipt:
        target = await self.version_snapshot(project_id, version_id)
        current = await self.snapshot(project_id)
        selected = set(file_ids)
        if selected - {file.file_id for file in target.files}:
            raise KeyError("File is not in this version")
        by_id = {file.file_id: file for file in current.files}
        ops: list[PaperOp] = []
        for file in target.files:
            if file.file_id not in selected:
                continue
            existing = by_id.get(file.file_id)
            data = target.file_bytes(file)
            if existing is not None and existing.path != file.path:
                ops.append(PaperOp(kind="rename", file_id=file.file_id, path=file.path))
            ops.append(
                PaperOp(
                    kind="write",
                    path=file.path,
                    base=current.file_bytes(existing) if existing is not None else None,
                    data=data,
                )
            )
        if not ops:
            return Receipt(revision=current.revision)
        return await self.apply(project_id, ops, actor, checkpoint=True)

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(limits.PAPER_TIMER_SECONDS)
            try:
                await self.tick()
            except Exception:
                _LOG.exception("Paper timer failed; pending work will retry")

    @_owned
    async def tick(self) -> None:
        """Save idle buffers, poll watched folders, and take automatic versions."""
        for project in list(self.projects):
            if self._fenced or project in self._paused or project in self._disposed:
                continue
            try:
                async with self._lock(project):
                    if project in self.projects:
                        await self._tick_project(project)
            except Exception:
                _LOG.exception("Paper pass failed for project %s", project)
        if time.monotonic() - self._last_collection >= limits.PAPER_HISTORY_MAX_SECONDS:
            self._last_collection = time.monotonic()
            await asyncio.to_thread(self._collect_bodies)

    async def _tick_project(self, project: str) -> None:
        state = self.projects[project]
        now = time.monotonic()
        rooms = self._project_rooms(project)
        for room in rooms:
            if (
                room.edited_at is not None
                and now - room.edited_at >= limits.PAPER_SAVE_IDLE_SECONDS
            ):
                await self._save(project, room)
        interval = (
            limits.PAPER_REMOTE_POLL_SECONDS if state.folder.remote else limits.PAPER_POLL_SECONDS
        )
        watched = state.turn is not None or any(room.subscribers for room in rooms)
        if watched and now - state.polled_at >= interval:
            await self._poll(project)
        if state.burst_at is not None and (
            now - state.changed_at >= limits.PAPER_HISTORY_IDLE_SECONDS
            or now - state.burst_at >= limits.PAPER_HISTORY_MAX_SECONDS
        ):
            await self._save_version(project, "automatic", None)

    def _collect_bodies(self) -> None:
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            collect_bodies(conn)
