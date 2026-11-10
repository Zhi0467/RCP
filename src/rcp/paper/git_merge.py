"""Deterministic path-wise three-tree merge, without persistent state."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rcp import limits
from rcp.paper.git_sync import GitEntry, GitError, _git
from rcp.paper.live import text_replacement
from rcp.paper.models import PaperOp, digest, paper_file_kind, paper_path
from rcp.rcp_home import rcp_temp_dir
from rcp.storage.paper_git_links import HeldFile, SkippedEntry


@dataclass
class MergeResult:
    tree: dict[str, GitEntry]
    ops: list[PaperOp]
    checks: list[dict[str, object]]
    held: list[HeldFile]
    skipped: list[SkippedEntry]
    proposals: dict[str, bytes]


def text_merge(base: bytes, ours: bytes, theirs: bytes) -> tuple[bytes, bytes | None]:
    with tempfile.TemporaryDirectory(dir=rcp_temp_dir()) as directory:
        root = Path(directory)
        paths = [root / name for name in ("ours", "base", "theirs")]
        for path, data in zip(paths, (ours, base, theirs), strict=True):
            path.write_bytes(data)
        args = [str(path) for path in paths]
        try:
            return _git(root, ["merge-file", "-p", *args]), None
        except GitError as exc:
            if not 0 < exc.returncode < 128:
                raise
        return (
            _git(root, ["merge-file", "--ours", "-p", *args]),
            _git(root, ["merge-file", "--theirs", "-p", *args]),
        )


def entry_reason(path: str, entry: GitEntry) -> str:
    try:
        paper_path(path)
    except (ValueError, UnicodeError):
        return "path"
    if entry.mode not in {"100644", "100755"}:
        return "mode"
    if (entry.size if entry.size is not None else len(entry.data)) > limits.PAPER_MAX_UPLOAD_BYTES:
        return "upload_limit"
    if paper_file_kind(path, entry.data) == "text" and len(entry.data) > limits.PAPER_MAX_DOC_BYTES:
        return "document_limit"
    return ""


def tree_reason(tree: dict[str, GitEntry]) -> str:
    if len(tree) > limits.PAPER_MAX_FILES:
        return "file_limit"
    if sum(len(entry.data) for entry in tree.values()) > limits.PAPER_MAX_TREE_BYTES:
        return "tree_limit"
    for path in tree:
        parts = path.split("/")
        if any("/".join(parts[:index]) in tree for index in range(1, len(parts))):
            return "collision"
    return ""


def _check(path: str, old: GitEntry, kind: Literal["text", "binary"]) -> dict[str, object]:
    check: dict[str, object] = {"file_id": old.file_id or path, "path": path, "kind": kind}
    check.update(
        {"text": old.data.decode()} if kind == "text" else {"blob_sha256": digest(old.data)}
    )
    return check


def _create(path: str, new: GitEntry, kind: Literal["text", "binary"]) -> PaperOp:
    return PaperOp(
        kind="create",
        path=path,
        file_kind=kind,
        text=new.data.decode() if kind == "text" else "",
        data=new.data if kind == "binary" else None,
    )


def _update(
    file_id: str, old: GitEntry, new: GitEntry, kind: Literal["text", "binary"]
) -> PaperOp | None:
    if kind == "text":
        return text_replacement(file_id, old.data.decode(), new.data.decode())
    return PaperOp(kind="binary", file_id=file_id, data=new.data)


def tree_ops(
    ours: dict[str, GitEntry], target: dict[str, GitEntry]
) -> tuple[list[PaperOp], list[dict[str, object]]]:
    deletes: list[PaperOp] = []
    edits: list[PaperOp | None] = []
    checks: list[dict[str, object]] = []
    for path in sorted(ours.keys() | target.keys()):
        old, new = ours.get(path), target.get(path)
        if old is not None and new is not None and old.data == new.data:
            continue
        old_kind = paper_file_kind(path, old.data) if old else None
        new_kind = paper_file_kind(path, new.data) if new else None
        if old is None:
            checks.append({"path_free": path})
        else:
            checks.append(_check(path, old, old_kind or "text"))
            if old_kind != new_kind:
                deletes.append(PaperOp(kind="delete", file_id=old.file_id or path))
        if new is None:
            continue
        if old is None or old_kind != new_kind:
            edits.append(_create(path, new, new_kind or "text"))
        else:
            edits.append(_update(old.file_id or path, old, new, new_kind or "text"))
    return deletes + [op for op in edits if op is not None], checks


def _candidate(
    path: str, ancestor: GitEntry | None, remote: GitEntry | None, local: GitEntry | None
) -> tuple[GitEntry | None, str, bytes | None]:
    old = ancestor.data if ancestor is not None else None
    if (local.data if local is not None else None) == old:
        return remote, "", None
    if (
        local is None
        or remote is None
        or any(
            paper_file_kind(path, entry.data) != "text"
            for entry in (ancestor, local, remote)
            if entry is not None
        )
    ):
        return local, "file", None
    merged, proposal = text_merge(old or b"", local.data, remote.data)
    if max(len(merged), len(proposal or b"")) > limits.PAPER_MAX_DOC_BYTES:
        return local, "file", None
    return (
        GitEntry(merged, local.mode, local.file_id),
        "text" if proposal is not None else "",
        proposal,
    )


def _fit_reason(
    trial: dict[str, GitEntry], proposals: dict[str, bytes], path: str, proposal: bytes | None
) -> str:
    reason = tree_reason(trial)
    if proposal is None and not proposals:
        return reason
    if proposal is not None:
        try:
            paper_path("files/" + path)
        except ValueError:
            return "change_path_limit"
        proposals = proposals | {path: proposal}
    return reason or tree_reason(trial | {name: GitEntry(body) for name, body in proposals.items()})


def merge_trees(
    base: dict[str, GitEntry], theirs: dict[str, GitEntry], ours: dict[str, GitEntry]
) -> MergeResult:
    result = {
        path: entry
        for path, entry in ours.items()
        if path in theirs or path not in base or entry.data != base[path].data
    }
    held: list[HeldFile] = []
    skipped: list[SkippedEntry] = []
    proposals: dict[str, bytes] = {}
    for path in sorted(base.keys() | theirs.keys() | ours.keys()):
        ancestor, remote, local = base.get(path), theirs.get(path), ours.get(path)
        if remote is not None and (reason := entry_reason(path, remote)):
            skipped.append({"path": path, "reason": reason})
            continue
        new = remote.data if remote is not None else None
        if new in (ancestor.data if ancestor else None, local.data if local else None):
            continue
        candidate, conflict, proposal = _candidate(path, ancestor, remote, local)
        trial = {name: entry for name, entry in result.items() if name != path}
        if candidate is not None:
            trial[path] = candidate
        if reason := _fit_reason(trial, proposals, path, proposal):
            if local is None:
                skipped.append({"path": path, "reason": reason})
                continue
            conflict, proposal, trial = "file", None, result
        result = trial
        if conflict:
            held.append(
                {
                    "file_id": local.file_id if local else "",
                    "path": path,
                    "base_commit": "",
                    "kind": "text" if conflict == "text" else "file",
                }
            )
        if proposal is not None:
            proposals[path] = proposal
    ops, checks = tree_ops(ours, result)
    return MergeResult(result, ops, checks, held, skipped, proposals)
