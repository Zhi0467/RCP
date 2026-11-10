"""Deterministic path-wise three-tree merge, without persistent state."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

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


def tree_ops(
    ours: dict[str, GitEntry], target: dict[str, GitEntry]
) -> tuple[list[PaperOp], list[dict[str, object]]]:
    deletes: list[PaperOp] = []
    edits: list[PaperOp] = []
    checks: list[dict[str, object]] = []
    for path in sorted(ours.keys() | target.keys()):
        old, new = ours.get(path), target.get(path)
        if old is not None and new is not None and old.data == new.data:
            continue
        old_kind = paper_file_kind(path, old.data) if old else None
        new_kind = paper_file_kind(path, new.data) if new else None
        file_id = old.file_id or path if old else ""
        if old is not None:
            check: dict[str, object] = {"file_id": file_id, "path": path, "kind": old_kind}
            check.update(
                {"text": old.data.decode()}
                if old_kind == "text"
                else {"blob_sha256": digest(old.data)}
            )
            checks.append(check)
        if old is not None and (new is None or old_kind != new_kind):
            deletes.append(PaperOp(kind="delete", file_id=file_id))
        if new is None:
            continue
        if old is None or old_kind != new_kind:
            if old is None:
                checks.append({"path_free": path})
            edits.append(
                PaperOp(
                    kind="create",
                    path=path,
                    file_kind=new_kind or "text",
                    text=new.data.decode() if new_kind == "text" else "",
                    data=new.data if new_kind == "binary" else None,
                )
            )
        elif new_kind == "text":
            op = text_replacement(file_id, old.data.decode(), new.data.decode())
            if op is not None:
                edits.append(op)
        else:
            edits.append(PaperOp(kind="binary", file_id=file_id, data=new.data))
    return deletes + edits, checks


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
        old = ancestor.data if ancestor is not None else None
        new = remote.data if remote is not None else None
        current = local.data if local is not None else None
        if new in (old, current):
            continue
        conflict = ""
        proposal = None
        if current == old:
            candidate = remote
        elif (
            local is not None
            and remote is not None
            and (ancestor is None or paper_file_kind(path, ancestor.data) == "text")
            and paper_file_kind(path, local.data) == paper_file_kind(path, remote.data) == "text"
        ):
            merged, proposal = text_merge(old or b"", current or b"", new or b"")
            candidate = GitEntry(merged, local.mode, local.file_id)
            conflict = "text" if proposal is not None else ""
            if len(merged) > limits.PAPER_MAX_DOC_BYTES or (
                proposal is not None and len(proposal) > limits.PAPER_MAX_DOC_BYTES
            ):
                candidate, conflict, proposal = local, "file", None
        else:
            candidate, conflict = local, "file"
        trial = dict(result)
        if candidate is None:
            trial.pop(path, None)
        else:
            trial[path] = candidate
        reason = tree_reason(trial)
        if proposal is not None:
            try:
                paper_path("files/" + path)
            except ValueError:
                reason = "change_path_limit"
        if proposals or proposal is not None:
            proposed = trial | {name: GitEntry(body) for name, body in proposals.items()}
            if proposal is not None:
                proposed[path] = GitEntry(proposal)
            reason = reason or tree_reason(proposed)
        if reason:
            if local is None and remote is not None:
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
