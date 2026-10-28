"""Validate coach files, review anchored changes, and apply guarded, durable receipts."""

from __future__ import annotations

import json
import logging
import os
import stat
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable
from contextlib import AbstractContextManager
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal, NamedTuple, cast

from pydantic import BaseModel, Field, PrivateAttr

from rcp.limits import (
    PAPER_CHANGES_DIFF_MAX_LINE_PAIRS,
    PAPER_CHANGES_MAX_FILE_BYTES,
    PAPER_CHANGES_MAX_FILES,
    PAPER_CHANGES_MAX_MOVES_BYTES,
    PAPER_CHANGES_MAX_TOTAL_BYTES,
    PAPER_CHANGES_PREVIEW_LINES,
)
from rcp.paper import PaperFile, PaperLive, PaperOp, PaperSnapshot
from rcp.paper.models import PaperPrecondition, digest, paper_file_kind, paper_path
from rcp.storage import AppStore

if TYPE_CHECKING:
    from rcp.service import ProjectService


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
    lines: dict[str, int] | None = None
    preview: dict[str, object] = Field(default_factory=dict)
    stats: dict[str, int] = Field(default_factory=dict)
    # Anchor mapped into the live text when the card was last read, in the UTF-16
    # offsets the browser's Yjs text uses, with its exact text; never stored.
    _current: dict[str, int | str] | None = PrivateAttr(default=None)


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
            card.update(
                state=self.state,
                decided_by=self.decided_by,
                decided_at=self.decided_at,
                current=change._current,
            )
            cards.append(card)
        return {**self.model_dump(exclude={"changes"}), "changes": cards}


def _path(value: str) -> str:
    try:
        return paper_path(value)
    except ValueError as exc:
        raise InvalidChanges([f"Invalid paper-relative path: {value!r}"]) from exc


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
        textual = (original.kind if original else paper_file_kind(name, body)) == "text"
        if textual:
            try:
                body.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise InvalidChanges([f"Text file is not UTF-8: {name}"]) from exc
    return files, pairs, deletions


# Edits this few unchanged lines apart become one card.
_CARD_MERGE_GAP_LINES = 3
# Past this share of changed lines, or this many cards, one rewrite card reads better.
_REWRITE_CHANGED_SHARE = 0.4
_REWRITE_CARD_COUNT = 12


class LineChange(NamedTuple):
    kind: Literal["edit", "rewrite"]
    start: int
    end: int
    before: str
    after: str
    first_line: int
    last_line: int


def _line_opcodes(old: list[str], new: list[str]) -> list[tuple[str, int, int, int, int]]:
    """Line opcodes that diff only the span between the common head and tail.

    Edits are local, and SequenceMatcher is quadratic in the lines it compares, so a
    middle past the budget is one replacement rather than a comparison.
    """
    head = 0
    while head < min(len(old), len(new)) and old[head] == new[head]:
        head += 1
    tail = 0
    while tail < min(len(old), len(new)) - head and old[-1 - tail] == new[-1 - tail]:
        tail += 1
    old_middle, new_middle = old[head : len(old) - tail], new[head : len(new) - tail]
    if len(old_middle) * len(new_middle) > PAPER_CHANGES_DIFF_MAX_LINE_PAIRS:
        middle = [("replace", 0, len(old_middle), 0, len(new_middle))]
    else:
        middle = SequenceMatcher(None, old_middle, new_middle, autojunk=False).get_opcodes()
    return [
        ("equal", 0, head, 0, head),
        *((tag, a + head, b + head, c + head, d + head) for tag, a, b, c, d in middle),
        ("equal", len(old) - tail, len(old), len(new) - tail, len(new)),
    ]


def line_changes(before: str, after: str) -> list[LineChange]:
    """Return deterministic offset ranges for review against the saved base."""
    old, new = before.splitlines(keepends=True), after.splitlines(keepends=True)
    edits = [op for op in _line_opcodes(old, new) if op[0] != "equal"]
    if not edits:
        return []
    regions: list[tuple[int, int, int, int]] = []
    for _tag, a, b, c, d in edits:
        if regions and a - regions[-1][1] <= _CARD_MERGE_GAP_LINES:
            first = regions.pop()
            regions.append((first[0], b, first[2], d))
        else:
            regions.append((a, b, c, d))
    changed = max(sum(b - a for _, a, b, _, _ in edits), sum(d - c for _, _, _, c, d in edits))
    rewrite = (
        changed / max(len(old), len(new), 1) > _REWRITE_CHANGED_SHARE
        or len(regions) > _REWRITE_CARD_COUNT
    )
    if rewrite:
        regions = [(0, len(old), 0, len(new))]
    offsets = [0]
    for line in old:
        offsets.append(offsets[-1] + len(line))
    return [
        LineChange(
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


class _RangeMapping:
    """One diff, with changed neighbourhoods bounded by unique unchanged lines."""

    def __init__(self, base: str, current: str):
        self.base, self.current = base, current
        old, new = base.splitlines(keepends=True), current.splitlines(keepends=True)
        old_counts, new_counts = Counter(old), Counter(new)
        old_offsets, new_offsets = [0], [0]
        for line in old:
            old_offsets.append(old_offsets[-1] + len(line))
        for line in new:
            new_offsets.append(new_offsets[-1] + len(line))
        self.edits: list[tuple[int, int, int, int]] = []
        left, right = 0, 0
        windows = []
        for tag, a, b, c, d in _line_opcodes(old, new):
            if tag != "equal":
                self.edits.append((old_offsets[a], old_offsets[b], new_offsets[c], new_offsets[d]))
                continue
            for i, j in zip(range(a, b), range(c, d), strict=True):
                if old_counts[old[i]] == new_counts[old[i]] == 1:
                    windows.append((left, old_offsets[i], right, new_offsets[j]))
                    left, right = old_offsets[i + 1], new_offsets[j + 1]
        windows.append((left, len(base), right, len(current)))
        self.changed = [(a, b, c, d) for a, b, c, d in windows if base[a:b] != current[c:d]]

    def map(self, start: int, end: int) -> tuple[int, int]:
        if not 0 <= start <= end <= len(self.base):
            raise OutdatedChange("Invalid base anchor")
        shift = 0
        for a, b, c, d in self.edits:
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
        if self.current[mapped[0] : mapped[1]] != self.base[start:end]:
            raise OutdatedChange("The anchored paper region changed")
        anchor = self.base[start:end]
        if anchor:
            for a, b, c, d in self.changed:
                if a < end and b > start:
                    for text in (self.base[a:b], self.current[c:d]):
                        first = text.find(anchor)
                        if first >= 0 and text.find(anchor, first + 1) >= 0:
                            raise OutdatedChange("The anchored paper region is ambiguous")
        return mapped


def _undo_against(
    current: PaperSnapshot, inverse: list[dict], guards: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Restore only the regions an accept changed, so later edits elsewhere in a file
    leave Undo available; any of those regions edited since refuses it."""
    by_id = {file.file_id: file for file in current.files}
    # Only a text replace narrows to regions; a created file's Undo deletes it, so its
    # whole-text guard stays and later typing in it refuses the Undo.
    replaced = {op["file_id"] for op in inverse if op["kind"] == "replace"}
    accepted = {
        guard["file_id"]: guard["text"]
        for guard in guards
        if guard.get("file_id") in replaced and isinstance(guard.get("text"), str)
    }
    ops: list[dict] = []
    checks = [
        {key: value for key, value in guard.items() if key != "text"}
        if guard.get("file_id") in accepted
        else guard
        for guard in guards
    ]
    for op in inverse:
        file = by_id.get(str(op.get("file_id")))
        if op["kind"] != "replace" or file is None or file.text is None:
            ops.append(op)
            continue
        if file.kind != "text":
            raise OutdatedChange("Paper file kind changed")
        after = accepted[op["file_id"]]
        mapping = _RangeMapping(after, file.text)
        # Right to left, so each region's offsets hold while the earlier ones apply.
        for _kind, start, end, before, restored, *_lines in reversed(
            line_changes(after, op["text"])
        ):
            start, end = mapping.map(start, end)
            checks.append({"file_id": op["file_id"], "start": start, "end": end, "text": before})
            ops.append(
                {
                    "kind": "replace",
                    "file_id": op["file_id"],
                    "start": start,
                    "end": end,
                    "text": restored,
                }
            )
    return ops, checks


def _proposal(change: Change) -> tuple[object, ...]:
    """What a card proposes, independent of its ids and anchors."""
    return (
        change.kind,
        change.path,
        change.new_path,
        change.before,
        change.after,
        change.before_blob,
        change.after_blob,
    )


def _preview(change: Change) -> None:
    removed, added = change.before.splitlines(), change.after.splitlines()
    if change.kind == "rename":
        # A rename keeps the text; its edits, if any, are their own cards.
        removed, added = [], []
    cap = PAPER_CHANGES_PREVIEW_LINES
    change.preview = {
        "removed": removed[:cap],
        "added": added[:cap],
        "more": max(0, len(removed) - cap) + max(0, len(added) - cap),
    }
    change.stats = {"added": len(added), "removed": len(removed)}


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def build_changes(
    task_id: str,
    snapshot: PaperSnapshot,
    root: Path,
    live: PaperLive,
    tree: PaperSnapshot | None = None,
) -> tuple[list[list[Change]], dict[str, bytes]]:
    """Diff the coach's output against ``snapshot``.

    ``tree`` is the whole saved tree at the same revision when ``snapshot`` holds
    only a refresh round's files; the result is validated against all of it.
    """
    tree = tree or snapshot
    files, renames, deletes = validate_output(root, snapshot)
    originals = {file.path: file for file in snapshot.files}
    reverse = {new: old for old, new in renames}
    changes: list[Change] = []
    moved = dict(renames)
    # Keyed by file id, so a path the coach could not see still collides.
    proposed = {
        file.file_id: file.model_copy(update={"path": moved.get(file.path, file.path)})
        for file in tree.files
        if file.path not in deletes
    }
    blobs = dict(tree._blobs)
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
            )
        )
    for name, body in sorted(files.items()):
        original = originals.get(reverse.get(name, name))
        # A replaced file keeps its kind; a new one is classified like an upload.
        file_kind = original.kind if original else paper_file_kind(name, body)
        text = body.decode("utf-8") if file_kind == "text" else None
        proposed[original.file_id if original else f"new:{name}"] = PaperFile(
            file_id=original.file_id if original else name,
            path=name,
            kind=file_kind,
            text=text,
            blob_sha256=digest(body) if file_kind == "binary" else None,
        )
        if file_kind == "binary":
            blobs[digest(body)] = body
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
                    after_blob=digest(body) if file_kind == "binary" else None,
                )
            )
        elif file_kind == "binary":
            if digest(body) != original.blob_sha256:
                changes.append(
                    Change(
                        task_id=task_id,
                        kind="binary",
                        file_id=original.file_id,
                        path=original.path,
                        base_revision=snapshot.revision,
                        file_kind="binary",
                        before_blob=original.blob_sha256,
                        after_blob=digest(body),
                    )
                )
        else:
            assert text is not None
            # Agent write tools add a final newline; one the human never typed is not a change.
            base = original.text or ""
            if base and not base.endswith("\n") and text.endswith("\n"):
                text = text[:-1]
            for kind, start, end, before, after, first, last in line_changes(base, text):
                changes.append(
                    Change(
                        task_id=task_id,
                        kind=kind,
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
    try:
        # Collection and live apply enforce the same complete-tree budgets.
        live.validate_tree(list(proposed.values()), blobs)
    except ValueError as exc:
        raise InvalidChanges([str(exc)]) from exc
    for change in changes:
        _preview(change)
    # Dependencies in prose/includes cannot be inferred reliably: group the entire
    # proposal when any rename exists, so its dependent edits never apply alone.
    groups = [changes] if renames and changes else [[change] for change in changes]
    return groups, {
        change.after_blob: blobs[change.after_blob] for change in changes if change.after_blob
    }


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

    def group(self, project_id: str, group_id: str) -> ChangeGroup:
        return self._load(project_id, group_id)

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
            **dict(row),
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
        tree = self.live.run_sync(self.live.snapshot(project_id, snapshot.revision))
        groups, blobs = build_changes(task_id, snapshot, changes_dir, self.live, tree)
        with self.admission(project_id), self.store.connection() as connection:
            decided: set[tuple[object, ...]] = set()
            for group_id in replace_group_ids or set():
                row = connection.execute(
                    "SELECT task_id,state FROM paper_change_groups WHERE project_id=? AND group_id=?",
                    (project_id, group_id),
                ).fetchone()
                if row is None or row[0] != (replace_source_task_id or task_id):
                    raise ValueError("Only this task's undecided groups can be refreshed")
                # A human decided it while the coach worked: the decision stands.
                if (
                    row[1] not in {"pending", "outdated"}
                    or connection.execute(
                        "SELECT 1 FROM paper_change_receipts WHERE project_id=? AND group_id=?",
                        (project_id, group_id),
                    ).fetchone()
                ):
                    decided |= {
                        _proposal(Change.model_validate_json(payload))
                        for (payload,) in connection.execute(
                            "SELECT payload FROM paper_changes WHERE project_id=? AND group_id=?",
                            (project_id, group_id),
                        )
                    }
                    continue
                connection.execute(
                    "DELETE FROM paper_change_groups WHERE project_id=? AND group_id=?",
                    (project_id, group_id),
                )
            connection.executemany(
                "INSERT OR IGNORE INTO paper_blobs VALUES(?,?,?)",
                [(sha, body, len(body)) for sha, body in blobs.items()],
            )
            # The refresh re-proposed whole files: a change a human decided meanwhile
            # is not offered again, and its atomic group goes with it.
            for changes in groups:
                if any(_proposal(change) in decided for change in changes):
                    continue
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
                        (
                            project_id,
                            change.change_id,
                            group_id,
                            change.model_dump_json(exclude_none=True),
                        ),
                    )
        return self.groups(project_id, task_id)

    def groups(self, project_id: str, task_id: str | None = None) -> list[ChangeGroup]:
        with self.store.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM paper_change_groups WHERE project_id=? "
                "AND (? IS NULL OR task_id=?) ORDER BY created_at,rowid",
                (project_id, task_id, task_id),
            ).fetchall()
            if not rows:
                return []
            cards: dict[str, list[Change]] = defaultdict(list)
            for row in connection.execute(
                "SELECT c.group_id,c.payload FROM paper_changes c "
                "JOIN paper_change_groups g ON g.project_id=c.project_id AND g.group_id=c.group_id "
                "WHERE g.project_id=? AND (? IS NULL OR g.task_id=?) ORDER BY c.rowid",
                (project_id, task_id, task_id),
            ):
                cards[row[0]].append(Change.model_validate_json(row[1]))
            # A crash between the live commit and the state write leaves the receipt as
            # the record of the decision.
            committed = {
                row[0]: json.loads(row[1])
                for row in connection.execute(
                    "SELECT group_id,payload FROM paper_change_receipts "
                    "WHERE project_id=? AND action='accept'",
                    (project_id,),
                )
            }
        groups = [ChangeGroup(**dict(row), changes=cards[row["group_id"]]) for row in rows]
        for group in groups:
            payload = committed.get(group.group_id)
            if group.state in {"pending", "outdated"} and payload and "receipt" in payload:
                self._state(project_id, group.group_id, payload["state"], payload["actor"])
                group.state = payload["state"]
        current = self._snapshot(project_id) if any(g.state == "pending" for g in groups) else None
        mappings: dict[tuple[str, int, int], _RangeMapping] = {}
        for group in groups:
            if group.state == "pending":
                assert current is not None
                try:
                    self._prepare(project_id, group, current, mappings)
                except OutdatedChange:
                    group.state = "outdated"
        return groups

    def stale_group_ids(self, project_id: str, task_id: str) -> set[str]:
        return {
            group.group_id
            for group in self.groups(project_id, task_id)
            if group.state == "outdated"
        }

    def undecided_group_ids(self, project_id: str, task_id: str, file_ids: set[str]) -> set[str]:
        """A refresh re-proposes whole files, so it replaces every undecided group of the
        task on those files, not only the outdated one."""
        return {
            group.group_id
            for group in self.groups(project_id, task_id)
            if group.state in {"pending", "outdated"}
            and any(change.file_id in file_ids for change in group.changes)
        }

    def affected_file_ids(self, project_id: str, group_ids: set[str]) -> set[str]:
        changes = [
            change for group_id in group_ids for change in self._load(project_id, group_id).changes
        ]
        paths = {path for change in changes for path in (change.path, change.new_path) if path}
        return {change.file_id for change in changes} | {
            file.file_id for file in self._snapshot(project_id).files if file.path in paths
        }

    def _prepare(
        self,
        project_id: str,
        group: ChangeGroup,
        current: PaperSnapshot,
        mappings: dict[tuple[str, int, int], _RangeMapping] | None = None,
    ) -> tuple[
        list[dict[str, object]],
        list[dict[str, object]],
        list[dict[str, object]],
        list[dict[str, object]],
    ]:
        if mappings is None:
            mappings = {}
        by_id = {file.file_id: file for file in current.files}
        paths = {file.path for file in current.files}
        checks: list[dict[str, object]] = []
        ops: list[dict[str, object]] = []
        original: dict[str, PaperFile | None] = {}
        resulting: dict[str, PaperFile] = {}
        # Edits are applied from right to left, preserving offsets in CURRENT text.
        ordered = sorted(
            group.changes, key=lambda c: (c.kind in {"edit", "rewrite"}, c.file_id, -c.start)
        )
        for change in ordered:
            file = by_id.get(change.file_id)
            if change.kind == "create":
                if (
                    file is not None
                    or change.path in paths
                    # A file cannot sit where a folder is, or inside a file.
                    or any(
                        path.startswith(f"{change.path}/") or change.path.startswith(f"{path}/")
                        for path in paths
                    )
                ):
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
            if file.file_id not in original:
                # The whole touched file, not the paper revision: edits elsewhere still
                # apply, and Undo's whole-file guard stays true to what was accepted.
                check: dict[str, object] = {
                    "file_id": file.file_id,
                    "path": file.path,
                    "kind": file.kind,
                }
                if file.kind == "text":
                    check["text"] = file.text
                checks.append(check)
            original.setdefault(file.file_id, file)
            resulting.setdefault(file.file_id, file.model_copy(deep=True))
            if change.kind in {"edit", "rewrite"}:
                key = (file.file_id, change.base_revision, current.revision)
                if key not in mappings:
                    base = self.live.run_sync(
                        self.live.text_at(project_id, file.file_id, change.base_revision)
                    )
                    mappings[key] = _RangeMapping(base, file.text or "")
                mapping = mappings[key]
                base = mapping.base
                start, end = mapping.map(change.start, change.end)
                if base[change.start : change.end] != change.before:
                    raise OutdatedChange("Stored paper anchor does not match its base")
                live_text = file.text or ""
                change._current = {
                    "start": _utf16_length(live_text[:start]),
                    "end": _utf16_length(live_text[:end]),
                    "line": live_text.count("\n", 0, start) + 1,
                    "revision": current.revision,
                    "text": change.before,
                }
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
                if change.kind == "delete":
                    ops.append({"kind": "delete", "file_id": file.file_id})
                    resulting.pop(file.file_id)
                else:
                    ops.append(
                        {
                            "kind": "binary",
                            "file_id": file.file_id,
                            "blob_sha256": change.after_blob,
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
                    }
                )
        return ops, checks, inverse, guards

    def _apply(self, project_id: str, payload: dict[str, object]) -> None:
        ops = []
        for item in cast(list[dict[str, object]], payload["ops"]):
            data = dict(item)
            data["text"] = data.get("text") or ""
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
                    "expect": {"receipt_key": key, "checks": checks},
                    "inverse": inverse,
                    "guards": guards,
                    "actor": actor,
                    "state": "applied" if directly else "accepted",
                }
                # Prepared before mutation, replayed verbatim after a crash. The live
                # core commits its receipt with the edit, so the gap before the final
                # state is harmless.
                self._save_receipt(project_id, group_id, key, "accept", payload)
            self._apply_prepared(project_id, key, payload)
            self._state(project_id, group_id, str(payload["state"]), str(payload["actor"]))
            return self._load(project_id, group_id)

    def _apply_prepared(self, project_id: str, key: str, payload: dict[str, object]) -> None:
        try:
            self._apply(project_id, payload)
        except Exception:
            # The live receipt can already be committed if delivery failed.
            # Preserve that result and inverse; only release prepared attempts.
            committed = self._receipt(project_id, key)
            if committed is not None and "receipt" in committed:
                return
            # A refused attempt is released, so a retry prepares it against the current text.
            with self.store.connection() as connection:
                connection.execute(
                    "DELETE FROM paper_change_receipts WHERE project_id=? AND receipt_key=?",
                    (project_id, key),
                )
            raise

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
                ops, checks = _undo_against(
                    self._snapshot(project_id),
                    cast(list[dict], accepted["inverse"]),
                    cast(list[dict], accepted["guards"]),
                )
                payload = {
                    "ops": ops,
                    "expect": {"receipt_key": key, "checks": checks},
                    "actor": actor,
                }
                self._save_receipt(project_id, group_id, key, "undo", payload)
            self._apply_prepared(project_id, key, payload)
            self._state(project_id, group_id, "undone", str(payload["actor"]))
            return self._load(project_id, group_id)

    def accept_all(
        self, project_id: str, task_id: str, actor: str, *, directly: bool = False
    ) -> list[ChangeGroup]:
        for group in self.groups(project_id, task_id):
            if group.state == "pending":
                try:
                    self.accept(project_id, group.group_id, actor, directly=directly)
                except Exception:
                    logging.getLogger(__name__).exception(
                        "Paper change group %s could not be applied", group.group_id
                    )
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


def get_paper_changes(owner: ProjectService) -> PaperChanges:
    """Return the service's durable saved-paper change manager."""
    return owner.paper_changes
