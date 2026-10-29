"""Loop-owned Paper documents; SQLite is saved truth, publication is an outbox.

No in-memory document is mutated until its candidate state has committed. Public
async methods marshal callers from worker loops to the server loop. Compaction
uses immutable bytes and an explicit sequence cutoff on a worker thread.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import tempfile
import time
import uuid
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from pycrdt import Doc, Text, create_update_message, get_update, handle_sync_message, merge_updates

from rcp import limits
from rcp.paper.models import (
    PaperConflict,
    PaperFile,
    PaperLimit,
    PaperOp,
    PaperPaused,
    PaperPrecondition,
    PaperRateLimit,
    PaperSnapshot,
    PaperTree,
    PaperTreeFile,
    Receipt,
    digest,
    file_format,
    paper_path,
)
from rcp.paper.persistence import (
    PaperSnapshotReader,
    collect_bodies,
    manifest_for,
    prune,
    read_published_tree,
    read_snapshot,
    save_version,
)
from rcp.storage import AppStore
from rcp.transport import StateUnavailable, StateWorkspace

_LOG = logging.getLogger(__name__)
_Result = TypeVar("_Result")


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


async def _settle(function, *args):
    """Finish worker work under the caller's lock; let it install before cancellation."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


def _doc(state: bytes | None = None, text: str | None = None) -> Doc:
    doc = Doc({"text": Text()}, skip_gc=False)
    if state is not None:
        doc.apply_update(state)
    if text:
        doc["text"].insert(0, text)
    return doc


def _compact_state(encoded: bytes) -> tuple[Doc, bytes]:
    doc = _doc(encoded)
    # Export integrated structs with GC, retaining only the unresolved tail from
    # the wire encoding. Doc.get_update alone loses out-of-order dependencies.
    pending = get_update(encoded, doc.get_state())
    return doc, merge_updates(doc.get_update(), pending)


def _ack(doc: Doc) -> bytes:
    # Type 100 is a single varuint byte; remaining payload is the state vector.
    return bytes([100]) + doc.get_state()


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


def text_replacement(file_id: str, old: str, new: str) -> PaperOp | None:
    """Keep unchanged CRDT characters (and peer positions) intact."""
    if old == new:
        return None
    start = 0
    while start < min(len(old), len(new)) and old[start] == new[start]:
        start += 1
    end, new_end = len(old), len(new)
    while end > start and new_end > start and old[end - 1] == new[new_end - 1]:
        end -= 1
        new_end -= 1
    return PaperOp(kind="replace", file_id=file_id, start=start, end=end, text=new[start:new_end])


@dataclass
class Room:
    doc: Doc
    seq: int
    encoded: bytes
    subscribers: set[asyncio.Queue[bytes]] = field(default_factory=set)
    awareness: dict[int, tuple[int, str]] = field(default_factory=dict)


def _stored_room(conn, key: tuple[str, str]) -> Room:
    state = conn.execute(
        "SELECT state,through_seq FROM paper_doc_states WHERE project_id=? AND file_id=?",
        key,
    ).fetchone()
    encoded = state[0] if state else b"\x00\x00"
    seq = state[1] if state else 0
    for row in conn.execute(
        'SELECT seq,"update" FROM paper_updates WHERE project_id=? '
        "AND file_id=? AND seq>? ORDER BY seq",
        (*key, seq),
    ):
        encoded = merge_updates(encoded, row[1])
        seq = row[0]
    if state is None and seq == 0:
        raise ValueError("Paper document state is missing")
    doc, encoded = _compact_state(encoded)
    return Room(doc, seq, encoded)


class PaperLive:
    def __init__(self, store: AppStore, workspace_for: Callable[[str], StateWorkspace]):
        self.store = store
        self.workspace_for = workspace_for
        self.loop: asyncio.AbstractEventLoop | None = None
        self.rooms: dict[tuple[str, str], Room] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._initialized: set[str] = set()
        self._paused: set[str] = set()
        self._disposed: set[str] = set()
        self._fenced = False
        self._timer: asyncio.Task | None = None
        self._publishers: dict[str, asyncio.Task] = {}
        self._publish_failed_at: dict[str, float] = {}
        self._compactions: set[asyncio.Task] = set()
        # Saved revision and time of each project's last prune; idle projects are
        # not rescanned, and continuous typing is rescanned only every interval.
        self._pruned: dict[str, tuple[int, float]] = {}
        self._last_collection = time.monotonic()
        self._rates: dict[str, deque[float]] = {}

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

    def _rows(self, sql, parameters=()):
        with self.store.connection() as conn:
            return conn.execute(sql, parameters).fetchall()

    def _lock(self, project: str) -> asyncio.Lock:
        return self._locks.setdefault(project, asyncio.Lock())

    def _writable(self, project: str) -> None:
        if self._fenced or project in self._paused:
            raise PaperPaused("Paper is paused")
        self._disposed.discard(project)

    async def start(self) -> None:
        if self.loop is not None and self.loop.is_closed():
            # An earlier lifespan's owner loop is gone with its rooms, locks and
            # timer; this lifespan starts a fresh owner instead of marshalling to it.
            self.__init__(self.store, self.workspace_for)
        await self._start()

    @_owned
    async def _start(self) -> None:
        if self._timer is None:
            rows = await asyncio.to_thread(self._rows, "SELECT project_id FROM projects")
            for (project,) in rows:
                try:
                    await asyncio.to_thread(self.store.require_project_accepts_new_work, project)
                except ValueError:
                    self._disposed.add(project)
                    continue
                try:
                    async with self._lock(project):
                        await self._ensure(project)
                except (KeyError, OSError, ValueError, StateUnavailable):
                    _LOG.exception("Paper migration deferred for project %s", project)
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
        for lock in list(self._locks.values()):
            async with lock:
                pass
        await asyncio.gather(*self._publishers.values(), *self._compactions, return_exceptions=True)
        for room in self.rooms.values():
            self._broadcast(room, b"")
        self.rooms.clear()

    @_owned
    async def pause(self, project_id: str) -> None:
        self._paused.add(project_id)
        async with self._lock(project_id):
            pass

    @_owned
    async def resume(self, project_id: str) -> None:
        # Undoes pause/dispose only; it never initializes, so it cannot fail on a
        # project whose paper was never opened. The next read initializes lazily.
        async with self._lock(project_id):
            try:
                await asyncio.to_thread(self.store.require_project_accepts_new_work, project_id)
            except ValueError:
                return
            self._disposed.discard(project_id)
            self._paused.discard(project_id)
            exists = await asyncio.to_thread(
                self._rows, "SELECT 1 FROM paper_publication WHERE project_id=?", (project_id,)
            )
            if exists:
                # Timed publication and history skip uninitialized projects.
                self._initialized.add(project_id)

    @_owned
    async def drain(self, project_id: str) -> None:
        async with self._lock(project_id):
            pass
        task = self._publishers.get(project_id)
        if task:
            await task
        # A failed compaction is logged by its callback; it must not stop the wait for the rest.
        await asyncio.gather(*self._compactions, return_exceptions=True)

    @_owned
    async def dispose(self, project_id: str) -> None:
        await self.pause(project_id)
        await self.drain(project_id)
        for key in [key for key in self.rooms if key[0] == project_id]:
            self._broadcast(self.rooms.pop(key), b"")
        self._initialized.discard(project_id)
        self._disposed.add(project_id)
        self._paused.discard(project_id)

    def _legacy_content(self, project: str) -> str | None:
        # Completed local display generation only; SSH refresh never blocks saving.
        workspace = self.workspace_for(project)
        with workspace.read_snapshot() as root:
            path = root / "paper" / "introduction.md"
            if path.is_symlink() or (root / "paper").is_symlink():
                raise ValueError("Legacy Paper cannot be a symlink")
            return path.read_text(encoding="utf-8") if path.exists() else None

    async def _ensure(self, project: str) -> None:
        if project in self._initialized:
            return
        if self._fenced:
            raise PaperPaused("Paper is shutting down")
        _, cancelled = await _settle(self._initialize, project)
        self._initialized.add(project)
        if cancelled:
            raise asyncio.CancelledError

    def _initialize(self, project):
        with self.store.connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM paper_publication WHERE project_id=?", (project,)
            ).fetchone()
            draft = conn.execute(
                "SELECT content, base_hash, ancestor_content FROM paper_drafts WHERE project_id=?",
                (project,),
            ).fetchone()
        if exists:
            return
        if project in self._disposed:
            raise PaperPaused("Paper is paused")

        canonical_unknown = False
        adopted: list[PaperFile] = []
        blobs: dict[str, bytes] = {}
        published = 0
        try:
            # Completed local display generation only; SSH refresh never blocks saving.
            with self.workspace_for(project).read_snapshot() as root:
                adopted, blobs, published = read_published_tree(root)
        except StateUnavailable:
            if draft is None:
                raise
            canonical_unknown = True
        # A database without Paper adopts the published tree rather than replacing it.
        files = [file for file in adopted if draft is None or file.path != "introduction.md"]
        introduction = next((file for file in adopted if file.path == "introduction.md"), None)
        if draft is not None and introduction is not None and introduction.kind != "text":
            raise ValueError("Legacy Paper introduction is not text")
        canonical = introduction.text if introduction is not None else None
        if draft is not None:
            introduction = PaperFile(
                file_id=str(uuid.uuid4()),
                path="introduction.md",
                kind="text",
                text=draft["content"],
            )
            files.append(introduction)
        self.validate_tree(files, blobs)
        behind = (
            draft is not None
            and canonical != draft["content"]
            and (digest(canonical.encode()) if canonical is not None else None)
            != draft["base_hash"]
        )
        now = self.store.now()
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Revisions continue after the adopted tree's, which publication never undercuts.
            conn.execute(
                "INSERT INTO paper_publication(project_id,saved_revision,published_revision) "
                "VALUES (?,?,?)",
                (project, published, published),
            )
            for sha, data in blobs.items():
                conn.execute(
                    "INSERT OR IGNORE INTO paper_blobs VALUES(?,?,?)", (sha, data, len(data))
                )
            for file in files:
                conn.execute(
                    "INSERT INTO paper_files(project_id,file_id,path,kind,created_at,blob_sha256) "
                    "VALUES(?,?,?,?,?,?)",
                    (project, file.file_id, file.path, file.kind, now, file.blob_sha256),
                )
                if file.kind == "text":
                    doc = _doc(text=file.text)
                    conn.execute(
                        "INSERT INTO paper_doc_states VALUES(?,?,?,?,?)",
                        (project, file.file_id, doc.get_update(), 0, published + 1),
                    )
            revision = published + 1 if files else published
            if files:
                self._revision(conn, project, revision, files, "migration", "migration", now)
                # Retain canonical and ancestor independently of the live draft.
                canonical_id = None
                for label, content in (
                    ("Legacy canonical", canonical),
                    ("Legacy ancestor", draft["ancestor_content"] if draft else None),
                ):
                    if (
                        introduction is not None
                        and content is not None
                        and content != introduction.text
                    ):
                        item = introduction.model_copy(update={"text": content})
                        version_id = save_version(
                            conn,
                            project,
                            PaperSnapshot(revision=0, files=[item]),
                            "migration",
                            now,
                            label,
                        )
                        if label == "Legacy canonical":
                            canonical_id = version_id
                if behind or canonical_unknown:
                    conn.execute(
                        "UPDATE paper_publication SET hold=? WHERE project_id=?",
                        (
                            json.dumps(
                                {
                                    "canonical_version": canonical_id,
                                    "canonical_unknown": canonical_unknown,
                                }
                            ),
                            project,
                        ),
                    )
                elif draft is None or canonical == draft["content"]:
                    conn.execute(
                        "UPDATE paper_publication SET published_revision=?,pending_since=NULL "
                        "WHERE project_id=?",
                        (revision, project),
                    )
            else:
                conn.execute(
                    "INSERT INTO paper_saved_snapshots VALUES(?,?,?)", (project, revision, "[]")
                )

    async def _room(self, project: str, file_id: str) -> Room:
        key = (project, file_id)
        if key in self.rooms:
            return self.rooms[key]
        if sum(key[0] == project for key in self.rooms) >= limits.PAPER_MAX_ROOMS_PER_PROJECT:
            # Inactive rooms are reloadable from SQLite.
            for old in list(self.rooms):
                if old[0] == project and not self.rooms[old].subscribers:
                    del self.rooms[old]
                    break
            else:
                raise PaperLimit("Too many open Paper rooms")
        room = await asyncio.to_thread(self._load_room, key)
        self.rooms[key] = room
        return room

    def _load_room(self, key):
        with self.store.connection() as conn:
            conn.execute("BEGIN")
            file = conn.execute(
                "SELECT kind FROM paper_files WHERE project_id=? AND file_id=? "
                "AND deleted_at IS NULL",
                key,
            ).fetchone()
            if file is None or file[0] != "text":
                raise KeyError(key[1])
            room = _stored_room(conn, key)
        return room

    def _snapshot(self, project: str, revision: int | None = None) -> PaperSnapshot:
        with self.store.connection() as conn:
            return PaperSnapshotReader(conn).snapshot(project, revision)

    @_owned
    async def snapshot(self, project_id: str, revision: int | None = None) -> PaperSnapshot:
        async with self._lock(project_id):
            await self._ensure(project_id)
            return await asyncio.to_thread(self._snapshot, project_id, revision)

    @_owned
    async def tree(self, project_id: str) -> PaperTree:
        async with self._lock(project_id):
            await self._ensure(project_id)
            return await asyncio.to_thread(self._tree, project_id)

    def _tree(self, project_id: str) -> PaperTree:
        with self.store.connection() as conn:
            conn.execute("BEGIN")
            revision, entries = PaperSnapshotReader(conn).metadata(project_id)
            row = conn.execute(
                "SELECT * FROM paper_publication WHERE project_id=?", (project_id,)
            ).fetchone()
            files = [
                PaperTreeFile(
                    file_id=entry["file_id"],
                    path=entry["path"],
                    kind=entry["kind"],
                    format=file_format(entry["path"], entry["kind"]),
                    size=entry["size"],
                    sha256=entry["text_sha256"]
                    if entry["kind"] == "text"
                    else entry["blob_sha256"],
                )
                for entry in entries
            ]
            return PaperTree(
                revision=revision,
                saved_revision=row["saved_revision"],
                published_revision=row["published_revision"],
                hold=row["hold"],
                files=files,
                publish_state=(
                    "published"
                    if row["saved_revision"] == row["published_revision"]
                    else "failed"
                    if row["last_error"]
                    else "pending"
                ),
            )

    @_owned
    async def raw_file(self, project_id: str, file_id: str, revision: int | None = None):
        async with self._lock(project_id):
            await self._ensure(project_id)
        return await asyncio.to_thread(self._raw_file, project_id, file_id, revision)

    def _raw_file(self, project_id, file_id, revision):
        with self.store.connection() as conn:
            conn.execute("BEGIN")
            return PaperSnapshotReader(conn).raw_file(project_id, file_id, revision)

    def file_exists(self, project_id: str, file_id: str) -> bool:
        with self.store.connection() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM paper_files WHERE project_id=? AND file_id=? AND deleted_at IS NULL",
                    (project_id, file_id),
                ).fetchone()
                is not None
            )

    def blob(self, sha256: str) -> bytes:
        with self.store.connection() as conn:
            row = conn.execute("SELECT bytes FROM paper_blobs WHERE sha256=?", (sha256,)).fetchone()
            if row is None:
                raise KeyError(sha256)
            return bytes(row[0])

    def materialize(self, snapshot: PaperSnapshot, dest: Path) -> None:
        PaperSnapshotReader.materialize(snapshot, dest)

    @_owned
    async def text_at(self, project_id: str, file_id: str, revision: int) -> str:
        file, data = await self.raw_file(project_id, file_id, revision)
        if file.kind != "text":
            raise KeyError(file_id)
        return data.decode()

    def _revision(self, conn, project, revision, files, actor, kind, now, manifest=None):
        conn.execute(
            "INSERT INTO paper_revisions VALUES(?,?,?,?,?)", (project, revision, now, actor, kind)
        )
        conn.execute(
            "UPDATE paper_publication SET saved_revision=?,pending_since=COALESCE(pending_since,?),"
            "last_edit_at=? WHERE project_id=?",
            (revision, now, now, project),
        )
        conn.execute(
            "INSERT INTO paper_saved_snapshots VALUES(?,?,?)",
            (project, revision, manifest if manifest is not None else manifest_for(conn, files)),
        )
        if kind != "text":
            prune(conn, project)

    def validate_tree(self, files: list[PaperFile], blobs: dict[str, bytes]) -> None:
        if len(files) > limits.PAPER_MAX_FILES:
            raise PaperLimit("Paper file limit reached")
        paths = sorted(paper_path(file.path) for file in files)
        if len(paths) != len(set(paths)):
            raise PaperPrecondition("Paper path is occupied")
        for path in paths:
            if any(path.startswith(other + "/") for other in paths if other != path):
                raise PaperPrecondition("Paper file blocks a directory")
        size = 0
        for file in files:
            if file.kind == "text":
                data = (file.text or "").encode()
                if b"\0" in data:
                    raise ValueError("Paper text cannot contain NUL")
                if len(data) > limits.PAPER_MAX_DOC_BYTES:
                    raise PaperLimit("Paper document is too large")
            else:
                sha = file.blob_sha256 or ""
                data = blobs[sha] if sha in blobs else self.blob(sha)
                if len(data) > limits.PAPER_MAX_UPLOAD_BYTES:
                    raise PaperLimit("Paper upload is too large")
            size += len(data)
        if size > limits.PAPER_MAX_TREE_BYTES:
            raise PaperLimit("Paper tree is too large")

    def _rate(self, project: str) -> None:
        now = time.monotonic()
        rate = self._rates.setdefault(project, deque())
        while rate and rate[0] < now - 1:
            rate.popleft()
        if len(rate) >= limits.PAPER_PROJECT_UPDATES_PER_SECOND:
            raise PaperRateLimit("Paper project update rate exceeded")
        rate.append(now)

    @staticmethod
    def _check_snapshot(files: dict[str, PaperFile], checks: list[dict]) -> None:
        for check in checks:
            if "path_free" in check and any(f.path == check["path_free"] for f in files.values()):
                raise PaperPrecondition("Paper path is occupied")
            if check.get("file_id_absent") in files:
                raise PaperPrecondition("Paper identity already exists")
            if "folder" in check:
                prefix = f"{check['folder']}/"
                held = {f.file_id for f in files.values() if f.path.startswith(prefix)}
                if held != set(check["file_ids"]):
                    raise PaperPrecondition("Paper folder changed")
            if "file_id" not in check:
                continue
            file = files.get(check["file_id"])
            if file is None:
                raise PaperPrecondition("Paper file no longer exists")
            for key in ("path", "kind", "blob_sha256"):
                if key in check and getattr(file, key) != check[key]:
                    raise PaperPrecondition(f"Paper {key} changed")
            if "text" in check:
                actual = file.text
                if "start" in check:
                    start, end = check["start"], check["end"]
                    if actual is None or not 0 <= start <= end <= len(actual):
                        raise PaperPrecondition("Paper region is invalid")
                    actual = actual[start:end]
                if actual != check["text"]:
                    raise PaperPrecondition("Paper text changed")

    @_owned
    async def apply(
        self,
        project_id: str,
        ops: list[PaperOp],
        actor: str,
        expect: dict | None = None,
        *,
        checkpoint: bool = False,
    ) -> Receipt:
        async with self._lock(project_id):
            self._writable(project_id)
            await self._ensure(project_id)
            result, cancelled = await _settle(
                self._apply, project_id, ops, actor, expect, checkpoint
            )
            receipt, updates, encoded_docs, deleted = result
            for file_id, (doc, seq, update) in updates.items():
                room = self.rooms.get((project_id, file_id))
                if room:
                    room.doc, room.seq, room.encoded = doc, seq, encoded_docs[file_id]
                    self._broadcast(room, create_update_message(update))
                    self._broadcast(room, _ack(doc))
                    self._compact(project_id, file_id, room, receipt.revision)
            for file_id in deleted:
                room = self.rooms.pop((project_id, file_id), None)
                if room:
                    self._broadcast(room, b"")
            if cancelled:
                raise asyncio.CancelledError
            return receipt

    def _apply(self, project_id, ops, actor, expect, checkpoint):
        if not ops:
            raise ValueError("Paper operations cannot be empty")
        expect = expect or {}
        key = expect.get("receipt_key")
        payload = {}
        if key:
            with self.store.connection() as conn:
                row = conn.execute(
                    "SELECT payload FROM paper_change_receipts "
                    "WHERE project_id=? AND receipt_key=?",
                    (project_id, key),
                ).fetchone()
            payload = json.loads(row[0]) if row else {}
            if "receipt" in payload:
                return Receipt.model_validate(payload["receipt"]), {}, {}, set()
        snapshot = self._snapshot(project_id)
        files = {file.file_id: file.model_copy() for file in snapshot.files}
        if "revision" in expect and expect["revision"] != snapshot.revision:
            raise PaperPrecondition("Paper revision changed")
        checks = expect.get("checks")
        if checks is not None:
            self._check_snapshot(files, checks)
        docs: dict[str, Doc] = {}
        bases: dict[str, Room] = {}
        blobs: dict[str, bytes] = {}
        affected = []
        for op in ops:
            file_id = op.file_id or str(uuid.uuid4())
            file = files.get(file_id)
            if op.kind == "create":
                if file is not None:
                    raise PaperPrecondition("Paper file already exists")
                # A guarded Undo may restore a tombstone, preserving its stable identity.
                with self.store.connection() as conn:
                    tombstone = conn.execute(
                        "SELECT kind FROM paper_files WHERE project_id=? AND file_id=?",
                        (project_id, file_id),
                    ).fetchone()
                    if tombstone and (
                        tombstone[0] != op.file_kind
                        or not any(check.get("file_id_absent") == file_id for check in checks or [])
                    ):
                        raise PaperPrecondition("Paper identity already exists")
                    restored = (
                        _stored_room(conn, (project_id, file_id))
                        if tombstone and tombstone[0] == "text"
                        else None
                    )
                file = PaperFile(
                    file_id=file_id,
                    path=paper_path(op.path or ""),
                    kind=op.file_kind,
                    text=op.text if op.file_kind == "text" else None,
                )
                files[file_id] = file
                if file.kind == "text":
                    if restored is not None:
                        if str(restored.doc["text"]) != op.text:
                            raise PaperPrecondition("Retained Paper document differs from Undo")
                        bases[file_id] = restored
                        docs[file_id] = _doc(restored.encoded)
                    else:
                        docs[file_id] = _doc(text=op.text)
            elif file is None:
                raise PaperPrecondition("Paper file no longer exists")
            if op.kind == "replace":
                if file.kind != "text":
                    raise ValueError("Binary files cannot receive text edits")
                current = file.text or ""
                start, end = op.start, op.end
                if start is None or end is None or not 0 <= start <= end <= len(current):
                    raise PaperPrecondition("Paper region is invalid")
                if not any(
                    check.get("file_id") == file_id
                    and "text" in check
                    and (
                        "start" not in check
                        or (check["start"] == start and check.get("end") == end)
                    )
                    for check in checks or []
                ):
                    raise ValueError("Paper text replacement requires a text precondition")
                if file_id not in docs:
                    bases[file_id] = self.rooms.get((project_id, file_id)) or self._load_room(
                        (project_id, file_id)
                    )
                    docs[file_id] = _doc(bases[file_id].encoded)
                doc = docs[file_id]
                # pycrdt Text offsets are UTF-8 bytes; the Python API uses characters.
                begin = len(current[:start].encode())
                finish = len(current[:end].encode())
                with doc.transaction():
                    del doc["text"][begin:finish]
                    doc["text"].insert(begin, op.text)
                file.text = str(doc["text"])
            elif op.kind == "rename":
                file.path = paper_path(op.path or "")
            elif op.kind == "delete":
                del files[file_id]
            if op.kind == "binary" or (op.kind == "create" and file.kind == "binary"):
                if file.kind != "binary":
                    raise ValueError("Use a binary Paper file for uploads")
                if op.data is not None:
                    sha = digest(op.data)
                    blobs[sha] = op.data
                else:
                    sha = op.blob_sha256 or ""
                    if sha not in snapshot._blobs:
                        blobs[sha] = self.blob(sha)
                file.blob_sha256 = sha
            affected.append(file_id)
        self.validate_tree(list(files.values()), snapshot._blobs | blobs)
        encoded_docs = {
            file_id: _compact_state(merge_updates(bases[file_id].encoded, doc.get_update()))[1]
            if file_id in bases
            else doc.get_update()
            for file_id, doc in docs.items()
        }
        if any(len(encoded) > limits.PAPER_MAX_MESSAGE_BYTES for encoded in encoded_docs.values()):
            raise PaperLimit("Paper CRDT state is too large")
        self._rate(project_id)
        revision = snapshot.revision + 1
        receipt = Receipt(revision=revision, receipt_key=key, file_ids=affected)
        now = self.store.now()
        updates = {}
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if (
                checkpoint
                and conn.execute(
                    "SELECT COALESCE(MAX(revision),-1) FROM paper_versions WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0]
                < snapshot.revision
            ):
                save_version(conn, project_id, snapshot, "automatic", now)
            for sha, data in blobs.items():
                conn.execute(
                    "INSERT OR IGNORE INTO paper_blobs VALUES(?,?,?)", (sha, data, len(data))
                )
            # Free old paths first so a validated multi-operation rename is atomic.
            conn.execute(
                "UPDATE paper_files SET deleted_at=? WHERE project_id=? AND deleted_at IS NULL",
                (now, project_id),
            )
            for file in files.values():
                conn.execute(
                    "INSERT INTO paper_files VALUES(?,?,?,?,?,NULL,?) ON CONFLICT(project_id,file_id) "
                    "DO UPDATE SET path=excluded.path,deleted_at=NULL,blob_sha256=excluded.blob_sha256",
                    (project_id, file.file_id, file.path, file.kind, now, file.blob_sha256),
                )
            for file_id, doc in docs.items():
                old = bases.get(file_id)
                seq = old.seq + 1 if old else 1
                update = doc.get_update(old.doc.get_state()) if old else doc.get_update()
                conn.execute(
                    "INSERT INTO paper_updates VALUES(?,?,?,?,?,?,?)",
                    (project_id, file_id, seq, update, revision, actor, now),
                )
                updates[file_id] = (doc, seq, update)
            self._revision(conn, project_id, revision, list(files.values()), actor, "apply", now)
            if key:
                payload.update(receipt=receipt.model_dump())
                conn.execute(
                    "INSERT INTO paper_change_receipts VALUES(?,?,NULL,'apply',?,?) "
                    "ON CONFLICT(project_id,receipt_key) DO UPDATE SET payload=excluded.payload",
                    (project_id, key, json.dumps(payload), now),
                )
        return receipt, updates, encoded_docs, set(affected) - files.keys()

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
    async def receive(
        self,
        project_id: str,
        file_id: str,
        message: bytes,
        actor: str,
        sender: asyncio.Queue[bytes] | None = None,
    ) -> list[bytes]:
        """Return the sender's replies; its own update is never echoed back to it."""
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
                return [reply, _ack(room.doc)] if reply else [_ack(room.doc)]
            seq = room.seq + 1
            result, cancelled = await _settle(
                self._receive_update, project_id, file_id, room.encoded, seq, update, actor
            )
            if result is None:
                if cancelled:
                    raise asyncio.CancelledError
                return [_ack(room.doc)]
            candidate, encoded, revision = result
            room.doc, room.seq, room.encoded = candidate, seq, encoded
            ack = _ack(candidate)
            self._broadcast(room, create_update_message(update), skip=sender)
            # Saved: an idle member is otherwise left showing a durable peer edit as unsaved.
            self._broadcast(room, ack, skip=sender)
            self._compact(project_id, file_id, room, revision)
            if cancelled:
                raise asyncio.CancelledError
            return [ack]

    def _receive_update(self, project, file_id, previous, seq, update, actor):
        encoded = merge_updates(previous, update)
        if encoded == previous:
            return None
        candidate, encoded = _compact_state(encoded)
        if len(encoded) > limits.PAPER_MAX_MESSAGE_BYTES:
            raise PaperLimit("Paper CRDT state is too large")
        self._rate(project)
        revision = self._save_text_update(
            project, file_id, seq, update, str(candidate["text"]), actor
        )
        return candidate, encoded, revision

    def _save_text_update(self, project, file_id, seq, update, text, actor):
        body = text.encode()
        if b"\0" in body:
            raise ValueError("Paper text cannot contain NUL")
        if len(body) > limits.PAPER_MAX_DOC_BYTES:
            raise PaperLimit("Paper document is too large")
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous, manifest = conn.execute(
                "SELECT p.saved_revision,s.manifest FROM paper_publication p "
                "JOIN paper_saved_snapshots s ON s.project_id=p.project_id "
                "AND s.revision=p.saved_revision WHERE p.project_id=?",
                (project,),
            ).fetchone()
            entries = json.loads(manifest)
            size = len(body)
            for entry in entries:
                if entry["file_id"] == file_id:
                    replacement = PaperFile(
                        file_id=file_id, path=entry["path"], kind="text", text=text
                    )
                    entry.update(json.loads(manifest_for(conn, [replacement]))[0])
                elif entry["kind"] == "binary":
                    size += conn.execute(
                        "SELECT size FROM paper_blobs WHERE sha256=?", (entry["blob_sha256"],)
                    ).fetchone()[0]
                else:
                    size += entry["size"]
            if size > limits.PAPER_MAX_TREE_BYTES:
                raise PaperLimit("Paper tree is too large")
            revision = previous + 1
            now = self.store.now()
            conn.execute(
                "INSERT INTO paper_updates VALUES(?,?,?,?,?,?,?)",
                (project, file_id, seq, update, revision, actor, now),
            )
            self._revision(
                conn,
                project,
                revision,
                [],
                actor,
                "text",
                now,
                manifest=json.dumps(entries, sort_keys=True),
            )
            return revision

    def _prune(self, project):
        with self.store.connection() as conn:
            prune(conn, project, refuse=False)

    @staticmethod
    def _broadcast(room: Room, message: bytes, skip: asyncio.Queue[bytes] | None = None) -> None:
        for queue in tuple(room.subscribers - {skip}):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # A slow client reconnects and resyncs from saved truth.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(b"")

    def _compact(self, project: str, file_id: str, room: Room, revision: int) -> None:
        if room.seq % limits.PAPER_COMPACT_UPDATES:
            return
        cutoff = room.seq
        task = asyncio.create_task(
            asyncio.to_thread(self._compact_bytes, project, file_id, cutoff, revision)
        )
        self._compactions.add(task)

        def completed(task):
            self._compactions.discard(task)
            if not task.cancelled() and task.exception():
                _LOG.error(
                    "Paper compaction failed; durable update tail retained: %s", task.exception()
                )

        task.add_done_callback(completed)

    def _compact_bytes(self, project, file_id, cutoff, revision):
        with self.store.connection() as conn:
            conn.execute("BEGIN")
            base = conn.execute(
                "SELECT state,through_seq FROM paper_doc_states WHERE project_id=? AND file_id=?",
                (project, file_id),
            ).fetchone()
            through = base[1] if base else 0
            if through >= cutoff:
                return
            updates = [base[0]] if base else []
            for row in conn.execute(
                'SELECT "update" FROM paper_updates WHERE project_id=? AND file_id=? AND seq>? AND seq<=? ORDER BY seq',
                (project, file_id, through, cutoff),
            ):
                updates.append(row[0])
            _, state = _compact_state(merge_updates(*updates))
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute(
                "SELECT 1 FROM paper_files WHERE project_id=? AND file_id=? AND deleted_at IS NULL",
                (project, file_id),
            ).fetchone():
                return
            conn.execute(
                "INSERT INTO paper_doc_states VALUES(?,?,?,?,?) ON CONFLICT(project_id,file_id) "
                "DO UPDATE SET state=excluded.state,through_seq=excluded.through_seq,"
                "through_revision=excluded.through_revision WHERE through_seq < excluded.through_seq",
                (project, file_id, state, cutoff, revision),
            )
            conn.execute(
                "DELETE FROM paper_updates WHERE project_id=? AND file_id=? AND seq<=?",
                (project, file_id, cutoff),
            )

    @_owned
    async def history(self, project_id: str) -> list[dict[str, Any]]:
        async with self._lock(project_id):
            await self._ensure(project_id)
            return await asyncio.to_thread(self._history, project_id)

    def _history(self, project_id):
        with self.store.connection() as conn:
            return [
                dict(row) | {"manifest": json.loads(row["manifest"])}
                for row in conn.execute(
                    "SELECT * FROM paper_versions WHERE project_id=? ORDER BY created_at DESC,rowid DESC",
                    (project_id,),
                )
            ]

    @_owned
    async def version_snapshot(self, project_id: str, version_id: str) -> PaperSnapshot:
        async with self._lock(project_id):
            await self._ensure(project_id)
            return await asyncio.to_thread(self._version_snapshot, project_id, version_id)

    def _version_snapshot(self, project_id: str, version_id: str) -> PaperSnapshot:
        with self.store.connection() as conn:
            row = conn.execute(
                "SELECT revision,manifest FROM paper_versions WHERE project_id=? AND version_id=?",
                (project_id, version_id),
            ).fetchone()
            if row is None:
                raise KeyError(version_id)
            return read_snapshot(conn, row[0], row[1])

    @_owned
    async def create_version(self, project_id: str, name: str, actor: str) -> dict[str, Any]:
        async with self._lock(project_id):
            self._writable(project_id)
            await self._ensure(project_id)
            version, cancelled = await _settle(self._create_version, project_id, name, actor)
            if cancelled:
                raise asyncio.CancelledError
        return version

    def _create_version(self, project_id, name, actor):
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            snapshot = PaperSnapshotReader(conn).snapshot(project_id)
            version_id = save_version(conn, project_id, snapshot, actor, self.store.now(), name)
            row = conn.execute(
                "SELECT * FROM paper_versions WHERE project_id=? AND version_id=?",
                (project_id, version_id),
            ).fetchone()
            return dict(row) | {"manifest": json.loads(row["manifest"])}

    @_owned
    async def delete_version(self, project_id: str, version_id: str) -> None:
        async with self._lock(project_id):
            self._writable(project_id)
            await self._ensure(project_id)
            _, cancelled = await _settle(self._delete_version, project_id, version_id)
            if cancelled:
                raise asyncio.CancelledError

    def _delete_version(self, project_id, version_id):
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
        ops, conditions = [], []
        for file in target.files:
            if file.file_id not in selected:
                continue
            existing = by_id.get(file.file_id)
            if existing is None:
                # A restored deleted file gets a fresh identity; old clients stay fenced.
                ops.append(
                    PaperOp(
                        kind="create",
                        path=file.path,
                        file_kind=file.kind,
                        text=file.text or "",
                        blob_sha256=file.blob_sha256,
                        data=target._blobs.get(file.blob_sha256 or ""),
                    )
                )
                conditions.append({"path_free": file.path})
                continue
            if file.kind == "text":
                op = text_replacement(file.file_id, existing.text or "", file.text or "")
                if op is not None:
                    ops.append(op)
                conditions.append({"file_id": file.file_id, "text": existing.text or ""})
            elif file.blob_sha256 != existing.blob_sha256:
                ops.append(
                    PaperOp(kind="binary", file_id=file.file_id, blob_sha256=file.blob_sha256)
                )
                conditions.append({"file_id": file.file_id, "blob_sha256": existing.blob_sha256})
            if file.path != existing.path:
                ops.append(PaperOp(kind="rename", file_id=file.file_id, path=file.path))
                conditions.append({"file_id": file.file_id, "path": existing.path})
        if not ops:
            return Receipt(revision=current.revision)
        return await self.apply(project_id, ops, actor, {"checks": conditions}, checkpoint=True)

    @_owned
    async def resolve_hold(self, project_id: str, keep: str, actor: str) -> PaperTree:
        async with self._lock(project_id):
            self._writable(project_id)
            await self._ensure(project_id)
            (row,) = await asyncio.to_thread(
                self._rows, "SELECT hold FROM paper_publication WHERE project_id=?", (project_id,)
            )
            hold = json.loads(row[0]) if row[0] else None
        if hold and hold.get("canonical_unknown"):
            # Either choice first retains the canonical text; publication would replace it.
            canonical = await asyncio.to_thread(self._legacy_content, project_id)
            async with self._lock(project_id):
                snapshot = await asyncio.to_thread(self._snapshot, project_id)
                introduction = next(
                    (file for file in snapshot.files if file.path == "introduction.md"), None
                )
                if canonical is not None and introduction is not None:
                    version, cancelled = await _settle(
                        self._save_canonical_version, project_id, introduction, canonical
                    )
                    hold["canonical_version"] = version
                    if cancelled:
                        raise asyncio.CancelledError
        if hold and keep == "canonical":
            if hold["canonical_version"]:
                target = await self.version_snapshot(project_id, hold["canonical_version"])
                await self.restore(
                    project_id,
                    hold["canonical_version"],
                    [file.file_id for file in target.files],
                    actor,
                )
            else:
                current = await self.snapshot(project_id)
                files = [file for file in current.files if file.path == "introduction.md"]
                if files:
                    # The checkpoint keeps the draft text the deletion removes.
                    await self.apply(
                        project_id,
                        [PaperOp(kind="delete", file_id=files[0].file_id)],
                        actor,
                        checkpoint=True,
                    )
        async with self._lock(project_id):
            self._writable(project_id)
            _, cancelled = await _settle(
                self._rows,
                "UPDATE paper_publication SET hold=NULL WHERE project_id=?",
                (project_id,),
            )
            if cancelled:
                raise asyncio.CancelledError
        return await self.tree(project_id)

    def _save_canonical_version(self, project, introduction, canonical):
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return save_version(
                conn,
                project,
                PaperSnapshot(
                    revision=0, files=[introduction.model_copy(update={"text": canonical})]
                ),
                "migration",
                self.store.now(),
                "Legacy canonical",
            )

    @_owned
    async def publish(self, project_id: str) -> None:
        """Flush a saved snapshot, leaving unreachable state roots pending."""
        if self._fenced or project_id in self._paused or project_id in self._disposed:
            raise PaperPaused("Paper is paused")
        task = self._publishers.get(project_id)
        if task is None:
            task = asyncio.create_task(self._publish(project_id))
            self._publishers[project_id] = task
        try:
            await asyncio.shield(task)
        finally:
            if task.done() and self._publishers.get(project_id) is task:
                del self._publishers[project_id]

    async def _publish(self, project: str) -> None:
        async with self._lock(project):
            await self._ensure(project)
            (row,) = await asyncio.to_thread(
                self._rows, "SELECT * FROM paper_publication WHERE project_id=?", (project,)
            )
            if row["hold"] or row["saved_revision"] == row["published_revision"]:
                return
            snapshot = await asyncio.to_thread(self._snapshot, project)

        def write():
            with tempfile.TemporaryDirectory(prefix="rcp-paper-") as directory:
                root = Path(directory)
                self.materialize(snapshot, root)
                self.workspace_for(project).publish_paper_tree(root, snapshot.revision)

        error = None
        try:
            await asyncio.to_thread(write)
        except Exception as exc:
            _LOG.warning("Paper publication remains pending for %s: %s", project, exc)
            error = str(exc)[:1000]
        async with self._lock(project):
            _, cancelled = await _settle(
                self._publication_result, project, snapshot.revision, error
            )
            if error is not None:
                self._publish_failed_at[project] = time.monotonic()
            else:
                self._publish_failed_at.pop(project, None)
            if cancelled:
                raise asyncio.CancelledError

    def _publication_result(self, project, revision, error):
        with self.store.connection() as conn:
            if error is not None:
                conn.execute(
                    "UPDATE paper_publication SET last_error=? WHERE project_id=?", (error, project)
                )
            else:
                conn.execute(
                    "UPDATE paper_publication SET published_revision=MAX(published_revision,?),"
                    "pending_since=CASE WHEN saved_revision=? THEN NULL ELSE "
                    "(SELECT MIN(created_at) FROM paper_revisions r WHERE r.project_id=paper_publication.project_id "
                    "AND r.revision>?) END,"
                    "last_error=NULL WHERE project_id=?",
                    (revision, revision, revision, project),
                )

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(limits.PAPER_TIMER_SECONDS)
            try:
                await self.tick()
            except Exception:
                _LOG.exception("Paper timer failed; pending work will retry")

    @_owned
    async def tick(self) -> None:
        """One timer pass; timestamps in SQLite also recover bursts after restart."""
        rows = await asyncio.to_thread(self._rows, "SELECT * FROM paper_publication")
        now = datetime.now(UTC)

        def age(value):
            return (now - datetime.fromisoformat(value)).total_seconds() if value else 0

        for row in rows:
            project = row["project_id"]
            if (
                self._fenced
                or project in self._paused
                or project in self._disposed
                or project not in self._initialized
            ):
                continue
            if (
                row["pending_since"]
                and (
                    not row["last_error"]
                    or time.monotonic()
                    - self._publish_failed_at.setdefault(project, time.monotonic())
                    >= limits.PAPER_PUBLISH_RETRY_SECONDS
                )
                and not row["hold"]
                and (
                    age(row["last_edit_at"]) >= limits.PAPER_PUBLISH_IDLE_SECONDS
                    or age(row["pending_since"]) >= limits.PAPER_PUBLISH_MAX_SECONDS
                )
                and project not in self._publishers
            ):
                task = asyncio.create_task(self._publish(project))
                self._publishers[project] = task

                def finished(task, project=project):
                    self._publishers.pop(project, None)
                    if not task.cancelled() and task.exception():
                        _LOG.error("Paper publisher failed: %s", task.exception())

                task.add_done_callback(finished)
            try:
                async with self._lock(project):
                    pruned = self._pruned.get(project)
                    if pruned is None or (
                        pruned[0] != row["saved_revision"]
                        and time.monotonic() - pruned[1] >= limits.PAPER_PRUNE_SECONDS
                    ):
                        pruning = asyncio.create_task(asyncio.to_thread(self._prune, project))
                        self._compactions.add(pruning)
                        pruning.add_done_callback(self._compactions.discard)
                        try:
                            await asyncio.shield(pruning)
                        except asyncio.CancelledError:
                            await pruning
                            raise
                        self._pruned[project] = (row["saved_revision"], time.monotonic())
                    saving = asyncio.create_task(
                        asyncio.to_thread(self._automatic_version, project, now)
                    )
                    self._compactions.add(saving)
                    saving.add_done_callback(self._compactions.discard)
                    try:
                        await asyncio.shield(saving)
                    except asyncio.CancelledError:
                        await saving
                        raise
            except Exception:
                # One project's history failure must not stop the others' timers.
                _LOG.exception("Paper history pass failed for project %s", project)

        if time.monotonic() - self._last_collection >= limits.PAPER_HISTORY_MAX_SECONDS:
            collection = asyncio.create_task(asyncio.to_thread(self._collect_bodies))
            self._compactions.add(collection)
            collection.add_done_callback(self._compactions.discard)
            await asyncio.shield(collection)
            self._last_collection = time.monotonic()

    def _collect_bodies(self):
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            collect_bodies(conn)

    def _automatic_version(self, project, now):
        with self.store.connection() as conn:
            row = conn.execute(
                "SELECT * FROM paper_publication WHERE project_id=?", (project,)
            ).fetchone()
            previous = conn.execute(
                "SELECT COALESCE(MAX(revision),0) FROM paper_versions WHERE project_id=?",
                (project,),
            ).fetchone()[0]
            burst = conn.execute(
                "SELECT MIN(created_at) FROM paper_revisions WHERE project_id=? AND revision>?",
                (project, previous),
            ).fetchone()[0]
            if (
                row["saved_revision"] > previous
                and burst
                and (
                    (now - datetime.fromisoformat(row["last_edit_at"])).total_seconds()
                    >= limits.PAPER_HISTORY_IDLE_SECONDS
                    or (now - datetime.fromisoformat(burst)).total_seconds()
                    >= limits.PAPER_HISTORY_MAX_SECONDS
                )
            ):
                conn.execute("BEGIN IMMEDIATE")
                snapshot = PaperSnapshotReader(conn).snapshot(project)
                save_version(conn, project, snapshot, "automatic", self.store.now())
