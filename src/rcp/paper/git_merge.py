"""Deterministic path-wise three-tree merge where GitHub wins every overlap."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from rcp import limits
from rcp.paper.git_sync import GitEntry, GitError, _git
from rcp.paper.models import PaperOp, paper_file_kind, paper_path
from rcp.rcp_home import rcp_temp_dir
from rcp.storage.paper_git_links import SkippedEntry


@dataclass
class MergeResult:
    tree: dict[str, GitEntry]
    ops: list[PaperOp]
    skipped: list[SkippedEntry]


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


def tree_ops(ours: dict[str, GitEntry], target: dict[str, GitEntry]) -> list[PaperOp]:
    """Writes that turn `ours` into `target`; each names the bytes it read as its base."""
    ops: list[PaperOp] = []
    for path in sorted(ours.keys() | target.keys()):
        old, new = ours.get(path), target.get(path)
        if old is not None and new is not None and old.data == new.data:
            continue
        if new is None:
            if old is not None:
                ops.append(PaperOp(kind="delete", file_id=old.file_id))
            continue
        ops.append(
            PaperOp(
                kind="write", path=path, base=old.data if old is not None else None, data=new.data
            )
        )
    return ops


def _candidate(
    path: str, ancestor: GitEntry | None, remote: GitEntry | None, local: GitEntry | None
) -> GitEntry | None:
    if local is None or remote is None or local.data == (ancestor.data if ancestor else None):
        return remote
    if any(
        paper_file_kind(path, entry.data) != "text"
        for entry in (ancestor, local, remote)
        if entry is not None
    ):
        return remote
    merged, theirs = text_merge(ancestor.data if ancestor else b"", local.data, remote.data)
    data = theirs if theirs is not None else merged
    if len(data) > limits.PAPER_MAX_DOC_BYTES:
        return remote
    return GitEntry(data, local.mode, local.file_id)


def merge_trees(
    base: dict[str, GitEntry], theirs: dict[str, GitEntry], ours: dict[str, GitEntry]
) -> MergeResult:
    result = {
        path: entry
        for path, entry in ours.items()
        if path in theirs or path not in base or entry.data != base[path].data
    }
    skipped: list[SkippedEntry] = []
    for path in sorted(base.keys() | theirs.keys() | ours.keys()):
        ancestor, remote, local = base.get(path), theirs.get(path), ours.get(path)
        if remote is not None and (reason := entry_reason(path, remote)):
            skipped.append({"path": path, "reason": reason})
            continue
        new = remote.data if remote is not None else None
        if new in (ancestor.data if ancestor else None, local.data if local else None):
            continue
        candidate = _candidate(path, ancestor, remote, local)
        trial = {name: entry for name, entry in result.items() if name != path}
        if candidate is not None:
            trial[path] = candidate
        if reason := tree_reason(trial):
            if local is None:
                skipped.append({"path": path, "reason": reason})
            continue
        result = trial
    return MergeResult(result, tree_ops(ours, result), skipped)
