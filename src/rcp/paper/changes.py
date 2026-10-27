"""Validate coach files, review anchored changes, and apply guarded, durable receipts."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import tempfile
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from pydantic import BaseModel, Field

from rcp.limits import (
    PAPER_CHANGES_MAX_FILE_BYTES,
    PAPER_CHANGES_MAX_FILES,
    PAPER_CHANGES_MAX_MOVES_BYTES,
    PAPER_CHANGES_MAX_TOTAL_BYTES,
    PAPER_CHANGES_PREVIEW_LINES,
)
from rcp.paper import PaperFile, PaperLive, PaperOp, PaperSnapshot
from rcp.paper.models import PaperPrecondition
from rcp.storage import AppStore


class InvalidChanges(ValueError):
    def __init__(self, diagnostics: list[str]) -> None:
        self.diagnostics = diagnostics
        super().__init__("; ".join(diagnostics))


class OutdatedChange(ValueError):
    pass


class Change(BaseModel):
    change_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    group_id: str = ""
    task_id: str
    kind: Literal["edit", "rewrite", "create", "rename", "delete", "binary"]
    file_id: str
    path: str
    new_path: str | None = None
    base_revision: int
    start: int = 0
    end: int = 0
    before: str = ""
    after: str = ""
    file_kind: Literal["text", "binary"] = "text"
    before_blob: str | None = None
    after_blob: str | None = None
    before_bytes: str | None = None
    after_bytes: str | None = None
    lines: dict[str, int] | None = None
    preview: dict[str, object] = Field(default_factory=dict)
    stats: dict[str, int] = Field(default_factory=dict)


class ChangeGroup(BaseModel):
    group_id: str
    task_id: str
    base_revision: int
    mode: Literal["review", "apply_directly"]
    state: Literal["pending", "accepted", "rejected", "outdated", "applied", "undone"]
    changes: list[Change]
    decided_by: str | None = None
    decided_at: str | None = None

    def public(self) -> dict[str, object]:
        cards = []
        for change in self.changes:
            card = change.model_dump(
                include={
                    "change_id",
                    "group_id",
                    "task_id",
                    "kind",
                    "file_id",
                    "path",
                    "new_path",
                    "lines",
                    "preview",
                    "stats",
                }
            )
            card.update(state=self.state, decided_by=self.decided_by, decided_at=self.decided_at)
            cards.append(card)
        return {**self.model_dump(exclude={"changes"}), "changes": cards}


def _path(value: str) -> str:
    parts = value.split("/")
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or len(value) > 1024
        or any(part in {"", ".", ".."} for part in parts)
        or PurePosixPath(value).is_absolute()
    ):
        raise InvalidChanges([f"Invalid paper-relative path: {value!r}"])
    return value


def _read_output(root: Path) -> dict[str, bytes]:
    """Descriptor-relative bounded walk: never follow a link, including the root."""
    result: dict[str, bytes] = {}
    total = 0
    entries = 0

    def visit(fd: int, prefix: str) -> None:
        nonlocal total, entries
        for name in sorted(os.listdir(fd)):
            entries += 1
            if entries > PAPER_CHANGES_MAX_FILES * 4:
                raise InvalidChanges(["Changes folder has too many entries"])
            relative = _path(f"{prefix}/{name}" if prefix else name)
            if not prefix and name not in {"files", "moves.json"}:
                raise InvalidChanges([f"Unexpected output: {name}"])
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    visit(child, relative)
                finally:
                    os.close(child)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise InvalidChanges([f"Not a regular unlinked file: {relative}"])
            cap = (
                PAPER_CHANGES_MAX_MOVES_BYTES
                if relative == "moves.json"
                else PAPER_CHANGES_MAX_FILE_BYTES
            )
            if info.st_size > cap or total + info.st_size > PAPER_CHANGES_MAX_TOTAL_BYTES:
                raise InvalidChanges([f"Changes byte limit exceeded: {relative}"])
            if len(result) >= PAPER_CHANGES_MAX_FILES + 1:
                raise InvalidChanges(["Too many changed files"])
            child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                opened = os.fstat(child)
                if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                    info.st_dev,
                    info.st_ino,
                ):
                    raise InvalidChanges([f"Changed during collection: {relative}"])
                with os.fdopen(child, "rb", closefd=False) as stream:
                    content = stream.read(cap + 1)
            finally:
                os.close(child)
            total += len(content)
            if len(content) > cap or total > PAPER_CHANGES_MAX_TOTAL_BYTES:
                raise InvalidChanges([f"Changes byte limit exceeded: {relative}"])
            result[relative] = content

    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            visit(fd, "")
        finally:
            os.close(fd)
    except OSError as exc:
        raise InvalidChanges([f"Cannot collect changes: {exc.strerror}"]) from exc
    return result


def validate_output(
    root: Path, snapshot: PaperSnapshot
) -> tuple[dict[str, bytes], list[tuple[str, str]], list[str]]:
    output = _read_output(root)
    unknown = [name for name in output if name != "moves.json" and not name.startswith("files/")]
    if unknown:
        raise InvalidChanges([f"Unexpected output: {name}" for name in unknown])
    try:
        moves = json.loads(output.get("moves.json", b"{}").decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise InvalidChanges(["moves.json must contain UTF-8 JSON"]) from exc
    if not isinstance(moves, dict) or set(moves) - {"rename", "delete"}:
        raise InvalidChanges(["moves.json requires only rename and delete arrays"])
    renames = moves.get("rename", [])
    deletes = moves.get("delete", [])
    if (
        not isinstance(renames, list)
        or not isinstance(deletes, list)
        or any(
            not isinstance(pair, list)
            or len(pair) != 2
            or any(not isinstance(p, str) for p in pair)
            for pair in renames
        )
        or any(not isinstance(p, str) for p in deletes)
    ):
        raise InvalidChanges(["Invalid rename or delete schema"])
    files = {
        name.removeprefix("files/"): body
        for name, body in output.items()
        if name.startswith("files/")
    }
    pairs = [(_path(pair[0]), _path(pair[1])) for pair in renames]
    deletions = [_path(name) for name in deletes]
    sources = [old for old, _new in pairs]
    targets = [new for _old, new in pairs]
    live = {file.path: file for file in snapshot.files}
    if (
        len(set(sources)) != len(sources)
        or len(set(targets)) != len(targets)
        or len(set(deletions)) != len(deletions)
    ):
        raise InvalidChanges(["Duplicate rename source, destination, or deletion"])
    if any(name not in live for name in sources + deletions):
        raise InvalidChanges(["Rename/delete source does not exist in the turn snapshot"])
    if any(name in live for name in targets):
        raise InvalidChanges(["Rename destination must be free in the turn snapshot"])
    if set(sources) & (set(deletions) | set(files)) or set(deletions) & (set(files) | set(targets)):
        raise InvalidChanges(
            [
                "A source cannot be both removed and written; write renamed content at its destination"
            ]
        )
    final_paths = (set(live) - set(sources) - set(deletions)) | set(targets) | set(files)
    if any(
        str(parent) in final_paths
        for name in final_paths
        for parent in PurePosixPath(name).parents
        if str(parent) != "."
    ):
        raise InvalidChanges(["A paper path cannot be both a file and a folder"])
    reverse = {new: old for old, new in pairs}
    for name, body in files.items():
        original = live.get(reverse.get(name, name))
        textual = (
            original.kind == "text"
            if original
            else Path(name).suffix.lower()
            in {
                ".md",
                ".tex",
                ".typ",
                ".bib",
                ".txt",
                ".json",
                ".csv",
                ".yaml",
                ".yml",
                ".html",
                ".svg",
            }
        )
        if textual:
            try:
                body.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise InvalidChanges([f"Text file is not UTF-8: {name}"]) from exc
    return files, pairs, deletions


def line_changes(before: str, after: str) -> list[tuple[str, int, int, str, str, int, int]]:
    """Return deterministic offset ranges, retaining identity even for repeated paragraphs."""
    old, new = before.splitlines(keepends=True), after.splitlines(keepends=True)
    edits = [
        op
        for op in SequenceMatcher(None, old, new, autojunk=False).get_opcodes()
        if op[0] != "equal"
    ]
    if not edits:
        return []
    regions: list[tuple[int, int, int, int]] = []
    for _tag, a, b, c, d in edits:
        if regions and a - regions[-1][1] <= 3:
            first = regions.pop()
            regions.append((first[0], b, first[2], d))
        else:
            regions.append((a, b, c, d))
    changed = max(sum(b - a for _, a, b, _, _ in edits), sum(d - c for _, _, _, c, d in edits))
    rewrite = changed / max(len(old), len(new), 1) > 0.4 or len(regions) > 12
    if rewrite:
        regions = [(0, len(old), 0, len(new))]
    offsets = [0]
    for line in old:
        offsets.append(offsets[-1] + len(line))
    return [
        (
            "rewrite" if rewrite else "edit",
            offsets[a],
            offsets[b],
            "".join(old[a:b]),
            "".join(new[c:d]),
            a + 1,
            max(a + 1, b),
        )
        for a, b, c, d in regions
    ]


def map_range(base: str, current: str, start: int, end: int) -> tuple[int, int]:
    """Translate base offsets; ambiguous boundary insertions and overlaps are stale."""
    if not 0 <= start <= end <= len(base):
        raise OutdatedChange("Invalid base anchor")
    shift = 0
    old_lines, new_lines = base.splitlines(keepends=True), current.splitlines(keepends=True)
    old_offsets, new_offsets = [0], [0]
    for line in old_lines:
        old_offsets.append(old_offsets[-1] + len(line))
    for line in new_lines:
        new_offsets.append(new_offsets[-1] + len(line))
    for tag, a, b, c, d in SequenceMatcher(
        None, old_lines, new_lines, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            continue
        a, b, c, d = old_offsets[a], old_offsets[b], new_offsets[c], new_offsets[d]
        overlaps = (
            (a == b and start <= a <= end)
            or (start == end and a <= start <= b)
            or (a < end and b > start)
        )
        if overlaps:
            raise OutdatedChange("The anchored paper region changed")
        if b <= start:
            shift += (d - c) - (b - a)
    mapped = (start + shift, end + shift)
    if current[mapped[0] : mapped[1]] != base[start:end]:
        raise OutdatedChange("The anchored paper region changed")
    return mapped


def _preview(change: Change) -> None:
    removed, added = change.before.splitlines(), change.after.splitlines()
    cap = PAPER_CHANGES_PREVIEW_LINES
    change.preview = {
        "removed": removed[:cap],
        "added": added[:cap],
        "more": max(0, len(removed) - cap) + max(0, len(added) - cap),
    }
    change.stats = {"added": len(added), "removed": len(removed)}


def _encoded(body: bytes) -> str:
    return base64.b64encode(body).decode("ascii")


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def build_changes(
    task_id: str, snapshot: PaperSnapshot, root: Path, live: PaperLive
) -> list[list[Change]]:
    files, renames, deletes = validate_output(root, snapshot)
    originals = {file.path: file for file in snapshot.files}
    reverse = {new: old for old, new in renames}
    changes: list[Change] = []
    binary_originals: dict[str, bytes] = {}
    wanted = set(deletes) | {reverse.get(path, path) for path in files}
    binary_files = [
        file for file in snapshot.files if file.kind == "binary" and file.path in wanted
    ]
    if binary_files:
        with tempfile.TemporaryDirectory(prefix="rcp-paper-base-") as temp:
            live.materialize(
                snapshot.model_copy(update={"files": binary_files}),
                Path(temp) / "files",
            )
            try:
                retained = _read_output(Path(temp))
            except InvalidChanges as exc:
                raise InvalidChanges(
                    [f"Saved binary base: {message}" for message in exc.diagnostics]
                ) from exc
            binary_originals = {
                path.removeprefix("files/"): body for path, body in retained.items()
            }
    for old, new in renames:
        file = originals[old]
        changes.append(
            Change(
                task_id=task_id,
                kind="rename",
                file_id=file.file_id,
                path=old,
                new_path=new,
                base_revision=snapshot.revision,
                before=file.text or "",
                file_kind=file.kind,
                before_blob=file.blob_sha256,
            )
        )
    for name in deletes:
        file = originals[name]
        changes.append(
            Change(
                task_id=task_id,
                kind="delete",
                file_id=file.file_id,
                path=name,
                base_revision=snapshot.revision,
                before=file.text or "",
                file_kind=file.kind,
                before_blob=file.blob_sha256,
                before_bytes=_encoded(binary_originals[name]) if file.kind == "binary" else None,
            )
        )
    for name, body in sorted(files.items()):
        original = originals.get(reverse.get(name, name))
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        binary_extension = Path(name).suffix.lower() in {
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
            ".pdf",
            ".zip",
            ".woff",
            ".woff2",
        }
        file_kind = (
            original.kind
            if original
            else "text"
            if text is not None and not binary_extension
            else "binary"
        )
        if original is None:
            changes.append(
                Change(
                    task_id=task_id,
                    kind="create",
                    file_id=str(uuid.uuid4()),
                    path=name,
                    base_revision=snapshot.revision,
                    after=text or "",
                    file_kind=file_kind,
                    after_blob=_sha(body) if file_kind == "binary" else None,
                    after_bytes=_encoded(body) if file_kind == "binary" else None,
                )
            )
        elif file_kind == "binary":
            if _sha(body) != original.blob_sha256:
                changes.append(
                    Change(
                        task_id=task_id,
                        kind="binary",
                        file_id=original.file_id,
                        path=original.path,
                        base_revision=snapshot.revision,
                        file_kind="binary",
                        before_blob=original.blob_sha256,
                        after_blob=_sha(body),
                        before_bytes=_encoded(binary_originals[original.path]),
                        after_bytes=_encoded(body),
                    )
                )
        else:
            assert text is not None
            for kind, start, end, before, after, first, last in line_changes(
                original.text or "", text
            ):
                changes.append(
                    Change(
                        task_id=task_id,
                        kind=cast(Literal["edit", "rewrite"], kind),
                        file_id=original.file_id,
                        path=original.path,
                        base_revision=snapshot.revision,
                        start=start,
                        end=end,
                        before=before,
                        after=after,
                        lines={"start": first, "end": last},
                    )
                )
    for change in changes:
        _preview(change)
    # Dependencies in prose/includes cannot be inferred reliably: group the entire
    # proposal when any rename exists, so its dependent edits never apply alone.
    return [changes] if renames and changes else [[change] for change in changes]


class PaperChanges:
    """The admission context must hold runtime/project mutation fences and recheck access."""

    def __init__(
        self,
        store: AppStore,
        live: PaperLive,
        admission: Callable[[str], AbstractContextManager[object]],
    ) -> None:
        self.store = store
        self.live = live
        self.admission = admission

    def _snapshot(self, project_id: str) -> PaperSnapshot:
        return self.live.run_sync(self.live.snapshot(project_id))

    def _load(self, project_id: str, group_id: str) -> ChangeGroup:
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT * FROM paper_change_groups WHERE project_id=? AND group_id=?",
                (project_id, group_id),
            ).fetchone()
            if row is None:
                raise KeyError("Paper change group not found")
            changes = connection.execute(
                "SELECT payload FROM paper_changes WHERE project_id=? AND group_id=? ORDER BY rowid",
                (project_id, group_id),
            ).fetchall()
        return ChangeGroup(
            **{
                key: row[key]
                for key in (
                    "group_id",
                    "task_id",
                    "base_revision",
                    "mode",
                    "state",
                    "decided_by",
                    "decided_at",
                )
            },
            changes=[Change.model_validate_json(item[0]) for item in changes],
        )

    def _state(self, project_id: str, group_id: str, state: str, actor: str | None = None) -> None:
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE paper_change_groups SET state=?, decided_by=?, decided_at=? WHERE project_id=? AND group_id=?",
                (state, actor, self.store.now() if actor else None, project_id, group_id),
            )

    def collect(
        self,
        project_id: str,
        task_id: str,
        snapshot: PaperSnapshot,
        changes_dir: Path,
        *,
        mode: Literal["review", "apply_directly"] = "review",
        replace_group_ids: set[str] | None = None,
        replace_source_task_id: str | None = None,
    ) -> list[ChangeGroup]:
        groups = build_changes(task_id, snapshot, changes_dir, self.live)
        with self.admission(project_id), self.store.connection() as connection:
            for group_id in replace_group_ids or set():
                row = connection.execute(
                    "SELECT task_id,state FROM paper_change_groups WHERE project_id=? AND group_id=?",
                    (project_id, group_id),
                ).fetchone()
                if (
                    row is None
                    or row[0] != (replace_source_task_id or task_id)
                    or row[1] not in {"pending", "outdated"}
                ):
                    raise ValueError("Only this task's undecided groups can be refreshed")
                if connection.execute(
                    "SELECT 1 FROM paper_change_receipts WHERE project_id=? AND group_id=?",
                    (project_id, group_id),
                ).fetchone():
                    raise ValueError("A group with a mutation receipt cannot be refreshed")
                connection.execute(
                    "DELETE FROM paper_change_groups WHERE project_id=? AND group_id=?",
                    (project_id, group_id),
                )
            for changes in groups:
                group_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO paper_change_groups VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        project_id,
                        group_id,
                        task_id,
                        snapshot.revision,
                        mode,
                        "pending",
                        None,
                        None,
                        self.store.now(),
                    ),
                )
                for change in changes:
                    change.group_id = group_id
                    connection.execute(
                        "INSERT INTO paper_changes VALUES (?,?,?,?)",
                        (project_id, change.change_id, group_id, change.model_dump_json()),
                    )
        return self.groups(project_id, task_id)

    def groups(self, project_id: str, task_id: str | None = None) -> list[ChangeGroup]:
        with self.admission(project_id):
            with self.store.connection() as connection:
                ids = connection.execute(
                    "SELECT group_id FROM paper_change_groups WHERE project_id=? AND (? IS NULL OR task_id=?) ORDER BY created_at,rowid",
                    (project_id, task_id, task_id),
                ).fetchall()
            result = []
            current = self._snapshot(project_id)
            for row in ids:
                group = self._load(project_id, row[0])
                if group.state == "pending":
                    try:
                        self._prepare(project_id, group, current)
                    except OutdatedChange:
                        self._state(project_id, group.group_id, "outdated")
                        group.state = "outdated"
                result.append(group)
            return result

    def stale_group_ids(self, project_id: str, task_id: str) -> set[str]:
        return {
            group.group_id
            for group in self.groups(project_id, task_id)
            if group.state == "outdated"
        }

    def affected_file_ids(self, project_id: str, group_ids: set[str]) -> set[str]:
        changes = [
            change for group_id in group_ids for change in self._load(project_id, group_id).changes
        ]
        paths = {path for change in changes for path in (change.path, change.new_path) if path}
        return {change.file_id for change in changes} | {
            file.file_id for file in self._snapshot(project_id).files if file.path in paths
        }

    def stale_file_ids(self, project_id: str, task_id: str) -> set[str]:
        return self.affected_file_ids(project_id, self.stale_group_ids(project_id, task_id))

    def _prepare(
        self, project_id: str, group: ChangeGroup, current: PaperSnapshot
    ) -> tuple[
        list[dict[str, object]],
        list[dict[str, object]],
        list[dict[str, object]],
        list[dict[str, object]],
    ]:
        by_id = {file.file_id: file for file in current.files}
        paths = {file.path for file in current.files}
        checks: list[dict[str, object]] = []
        ops: list[dict[str, object]] = []
        original: dict[str, PaperFile | None] = {}
        resulting: dict[str, PaperFile] = {}
        retained: dict[str, str | None] = {}
        # Edits are applied from right to left, preserving offsets in CURRENT text.
        ordered = sorted(
            group.changes, key=lambda c: (c.kind in {"edit", "rewrite"}, c.file_id, -c.start)
        )
        for change in ordered:
            file = by_id.get(change.file_id)
            if change.kind == "create":
                if change.path in paths or file is not None:
                    raise OutdatedChange("Created paper path is no longer free")
                checks.append({"path_free": change.path})
                ops.append(
                    {
                        "kind": "create",
                        "file_id": change.file_id,
                        "path": change.path,
                        "file_kind": change.file_kind,
                        "text": change.after if change.file_kind == "text" else None,
                        "blob_sha256": change.after_blob,
                        "bytes_base64": change.after_bytes,
                    }
                )
                original[change.file_id] = None
                resulting[change.file_id] = PaperFile(
                    file_id=change.file_id,
                    path=change.path,
                    kind=change.file_kind,
                    text=change.after if change.file_kind == "text" else None,
                    blob_sha256=change.after_blob,
                )
                continue
            if file is None or file.path != change.path or file.kind != change.file_kind:
                raise OutdatedChange("Paper file identity or path changed")
            original.setdefault(file.file_id, file)
            resulting.setdefault(file.file_id, file.model_copy(deep=True))
            checks.append({"file_id": file.file_id, "path": file.path, "kind": file.kind})
            if change.kind in {"edit", "rewrite"}:
                base = self.live.run_sync(
                    self.live.text_at(project_id, file.file_id, change.base_revision)
                )
                start, end = map_range(base, file.text or "", change.start, change.end)
                if base[change.start : change.end] != change.before:
                    raise OutdatedChange("Stored paper anchor does not match its base")
                checks.append(
                    {"file_id": file.file_id, "start": start, "end": end, "text": change.before}
                )
                ops.append(
                    {
                        "kind": "replace",
                        "file_id": file.file_id,
                        "start": start,
                        "end": end,
                        "text": change.after,
                    }
                )
                text = resulting[file.file_id].text or ""
                resulting[file.file_id].text = text[:start] + change.after + text[end:]
            elif change.kind == "rename":
                if change.new_path in paths:
                    raise OutdatedChange("Rename destination is no longer free")
                if (
                    file.text != (change.before if file.kind == "text" else None)
                    or file.blob_sha256 != change.before_blob
                ):
                    raise OutdatedChange("Rename source changed")
                checks.extend(
                    [
                        {
                            "file_id": file.file_id,
                            "text": file.text,
                            "blob_sha256": file.blob_sha256,
                        },
                        {"path_free": change.new_path},
                    ]
                )
                ops.append({"kind": "rename", "file_id": file.file_id, "path": change.new_path})
                assert change.new_path is not None
                resulting[file.file_id].path = change.new_path
            else:
                if (
                    file.text != (change.before if file.kind == "text" else None)
                    or file.blob_sha256 != change.before_blob
                ):
                    raise OutdatedChange("Paper file content changed")
                checks.append(
                    {"file_id": file.file_id, "text": file.text, "blob_sha256": file.blob_sha256}
                )
                retained[file.file_id] = change.before_bytes
                if change.kind == "delete":
                    ops.append({"kind": "delete", "file_id": file.file_id})
                    resulting.pop(file.file_id)
                else:
                    ops.append(
                        {
                            "kind": "binary",
                            "file_id": file.file_id,
                            "blob_sha256": change.after_blob,
                            "bytes_base64": change.after_bytes,
                        }
                    )
                    resulting[file.file_id].blob_sha256 = change.after_blob
        inverse: list[dict[str, object]] = []
        guards: list[dict[str, object]] = []
        for file_id, old in original.items():
            new = resulting.get(file_id)
            if new is None:
                assert old is not None
                guards.append({"path_free": old.path, "file_id_absent": file_id})
                inverse.append(
                    {
                        "kind": "create",
                        "file_id": file_id,
                        "path": old.path,
                        "file_kind": old.kind,
                        "text": old.text,
                        "blob_sha256": old.blob_sha256,
                        "bytes_base64": retained.get(file_id),
                    }
                )
                continue
            guards.append(
                {
                    "file_id": file_id,
                    "path": new.path,
                    "kind": new.kind,
                    "text": new.text,
                    "blob_sha256": new.blob_sha256,
                }
            )
            if old is None:
                inverse.append({"kind": "delete", "file_id": file_id})
                continue
            if old.path != new.path:
                guards.append({"path_free": old.path})
                inverse.append({"kind": "rename", "file_id": file_id, "path": old.path})
            if old.kind == "text" and old.text != new.text:
                inverse.append(
                    {
                        "kind": "replace",
                        "file_id": file_id,
                        "start": 0,
                        "end": len(new.text or ""),
                        "text": old.text,
                    }
                )
            elif old.kind == "binary" and old.blob_sha256 != new.blob_sha256:
                inverse.append(
                    {
                        "kind": "binary",
                        "file_id": file_id,
                        "blob_sha256": old.blob_sha256,
                        "bytes_base64": retained.get(file_id),
                    }
                )
        return ops, checks, inverse, guards

    def _apply(self, project_id: str, payload: dict[str, object]) -> None:
        ops = []
        for item in cast(list[dict[str, object]], payload["ops"]):
            data = dict(item)
            encoded = data.pop("bytes_base64", None)
            if encoded is not None:
                data["data"] = base64.b64decode(str(encoded), validate=True)
            data["text"] = data.get("text") or ""
            # Prepared receipts from the separate coach slice used this spelling.
            if data["kind"] == "text":
                data["kind"] = "replace"
            ops.append(PaperOp.model_validate(data))
        try:
            self.live.run_sync(
                self.live.apply(
                    project_id,
                    ops,
                    actor=str(payload["actor"]),
                    expect=cast(dict[str, object], payload["expect"]),
                )
            )
        except PaperPrecondition as exc:
            raise OutdatedChange(str(exc)) from exc

    def proposed(self, project_id: str, change_id: str) -> str:
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT payload FROM paper_changes WHERE project_id=? AND change_id=?",
                (project_id, change_id),
            ).fetchone()
        if row is None:
            raise KeyError(change_id)
        change = Change.model_validate_json(row[0])
        if change.kind not in {"rewrite", "create"} or change.file_kind != "text":
            raise ValueError("This card does not contain a full proposed text file")
        return change.after

    def _receipt(self, project_id: str, key: str) -> dict[str, object] | None:
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT payload FROM paper_change_receipts WHERE project_id=? AND receipt_key=?",
                (project_id, key),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def _save_receipt(
        self, project_id: str, group_id: str, key: str, action: str, payload: dict[str, object]
    ) -> None:
        with self.store.connection() as connection:
            connection.execute(
                "INSERT INTO paper_change_receipts VALUES (?,?,?,?,?,?)",
                (project_id, key, group_id, action, json.dumps(payload), self.store.now()),
            )

    def accept(
        self, project_id: str, group_id: str, actor: str, *, directly: bool = False
    ) -> ChangeGroup:
        with self.admission(project_id):
            group = self._load(project_id, group_id)
            if group.state in {"accepted", "applied"}:
                return group
            if group.state not in {"pending", "outdated"}:
                raise ValueError("Only undecided paper changes can be accepted")
            if directly and group.mode != "apply_directly":
                raise ValueError("This task did not authorize direct application")
            key = f"paper:{group_id}:accept"
            payload = self._receipt(project_id, key)
            if payload is None:
                current = self._snapshot(project_id)
                try:
                    ops, checks, inverse, guards = self._prepare(project_id, group, current)
                except OutdatedChange:
                    self._state(project_id, group_id, "outdated")
                    raise
                payload = {
                    "ops": ops,
                    "expect": {"receipt_key": key, "revision": current.revision, "checks": checks},
                    "inverse": inverse,
                    "guards": guards,
                    "actor": actor,
                    "state": "applied" if directly else "accepted",
                }
                # Prepared before mutation, replayed verbatim after a crash. S1's
                # atomic receipt makes the gap before the final state harmless.
                self._save_receipt(project_id, group_id, key, "accept", payload)
            try:
                self._apply(project_id, payload)
            except OutdatedChange:
                # The live core raises this only for an atomic precondition refusal,
                # never for an uncertain transport/result failure.
                with self.store.connection() as connection:
                    connection.execute(
                        "DELETE FROM paper_change_receipts WHERE project_id=? AND receipt_key=?",
                        (project_id, key),
                    )
                self._state(project_id, group_id, "outdated")
                raise
            self._state(project_id, group_id, str(payload["state"]), str(payload["actor"]))
            return self._load(project_id, group_id)

    def reject(self, project_id: str, group_id: str, actor: str) -> ChangeGroup:
        with self.admission(project_id):
            group = self._load(project_id, group_id)
            if group.state == "rejected":
                return group
            if group.state not in {"pending", "outdated"} or self._receipt(
                project_id, f"paper:{group_id}:accept"
            ):
                raise ValueError("A decided or applying group cannot be rejected")
            self._state(project_id, group_id, "rejected", actor)
            return self._load(project_id, group_id)

    def undo(self, project_id: str, group_id: str, actor: str) -> ChangeGroup:
        with self.admission(project_id):
            group = self._load(project_id, group_id)
            if group.state == "undone":
                return group
            if group.state not in {"accepted", "applied"}:
                raise ValueError("Only applied paper changes can be undone")
            key = f"paper:{group_id}:undo"
            payload = self._receipt(project_id, key)
            if payload is None:
                accepted = self._receipt(project_id, f"paper:{group_id}:accept")
                if accepted is None:
                    raise ValueError("The acceptance receipt is missing")
                payload = {
                    "ops": accepted["inverse"],
                    "expect": {"receipt_key": key, "checks": accepted["guards"]},
                    "actor": actor,
                }
                self._save_receipt(project_id, group_id, key, "undo", payload)
            self._apply(project_id, payload)
            self._state(project_id, group_id, "undone", str(payload["actor"]))
            return self._load(project_id, group_id)

    def accept_all(
        self, project_id: str, task_id: str, actor: str, *, directly: bool = False
    ) -> list[ChangeGroup]:
        for group in self.groups(project_id, task_id):
            if group.state == "pending":
                try:
                    self.accept(project_id, group.group_id, actor, directly=directly)
                except OutdatedChange:
                    continue
        return self.groups(project_id, task_id)

    def undo_all(self, project_id: str, task_id: str, actor: str) -> list[ChangeGroup]:
        # Acceptance order, not creation order: later groups may depend on earlier ones.
        groups = sorted(
            self.groups(project_id, task_id), key=lambda group: group.decided_at or "", reverse=True
        )
        for group in groups:
            if group.state in {"accepted", "applied"}:
                self.undo(project_id, group.group_id, actor)
        return self.groups(project_id, task_id)


def get_paper_changes(owner: object) -> PaperChanges:
    """Return the service's durable saved-paper change manager."""
    manager = getattr(owner, "paper_changes", None)
    if not isinstance(manager, PaperChanges):
        raise ValueError("Paper change service is unavailable")
    return manager
