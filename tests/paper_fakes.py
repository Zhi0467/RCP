"""Saved-tree fake for the S3/S1 protocol boundary; no canonical-file fallback."""

from __future__ import annotations

import base64
from pathlib import Path

from rcp.paper.changes import OutdatedChange
from rcp.paper.coach_seam import SavedPaperFile, SavedPaperSnapshot


class FakePaperLive:
    def __init__(self, snapshot: SavedPaperSnapshot) -> None:
        self.current = snapshot.model_copy(deep=True)
        self.history = {snapshot.revision: snapshot.model_copy(deep=True)}
        self.blobs: dict[str, bytes] = {}
        self.receipts: dict[str, object] = {}
        self.applied: list[dict[str, object]] = []

    def snapshot(self, project_id: str, revision: int | None = None) -> SavedPaperSnapshot:
        return (self.current if revision is None else self.history[revision]).model_copy(deep=True)

    def materialize(self, snapshot: SavedPaperSnapshot, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        for file in snapshot.files:
            path = dest / file.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(
                (file.text or "").encode()
                if file.kind == "text"
                else self.blobs[file.blob_sha256 or ""]
            )

    def text_at(self, project_id: str, file_id: str, revision: int) -> str:
        return next(
            file.text or "" for file in self.history[revision].files if file.file_id == file_id
        )

    def human_edit(self, file_id: str, text: str) -> None:
        next(file for file in self.current.files if file.file_id == file_id).text = text
        self.current.revision += 1
        self.history[self.current.revision] = self.current.model_copy(deep=True)

    def apply(
        self, project_id: str, ops: list[dict[str, object]], actor: str, expect: dict[str, object]
    ) -> object:
        key = str(expect["receipt_key"])
        if key in self.receipts:
            return self.receipts[key]
        files = {file.file_id: file for file in self.current.files}
        paths = {file.path for file in files.values()}
        if "revision" in expect and expect["revision"] != self.current.revision:
            raise OutdatedChange("Revision changed")
        checks = expect.get("checks", [])
        assert isinstance(checks, list)
        for check in checks:
            assert isinstance(check, dict)
            if "path_free" in check and check["path_free"] in paths:
                raise OutdatedChange("Path occupied")
            if "file_id_absent" in check and check["file_id_absent"] in files:
                raise OutdatedChange("Identity occupied")
            if "file_id" not in check:
                continue
            file = files.get(str(check["file_id"]))
            if file is None:
                raise OutdatedChange("File disappeared")
            for field in ("path", "kind", "blob_sha256"):
                if field in check and getattr(file, field) != check[field]:
                    raise OutdatedChange(f"{field} changed")
            if "text" in check:
                actual = (
                    (file.text or "")[check["start"] : check["end"]]
                    if "start" in check
                    else file.text
                )
                if actual != check["text"]:
                    raise OutdatedChange("Text changed")
        updated = self.current.model_copy(deep=True)
        files = {file.file_id: file for file in updated.files}
        for op in ops:
            file_id = str(op["file_id"])
            kind = op["kind"]
            if kind == "create":
                files[file_id] = SavedPaperFile(
                    file_id=file_id,
                    path=str(op["path"]),
                    kind=op["file_kind"],
                    text=op.get("text"),
                    blob_sha256=op.get("blob_sha256"),
                )
            elif kind == "delete":
                del files[file_id]
            elif kind == "rename":
                files[file_id].path = str(op["path"])
            elif kind == "text":
                before = files[file_id].text or ""
                files[file_id].text = (
                    before[0 : int(op["start"])] + str(op["text"]) + before[int(op["end"]) :]
                )
            elif kind == "binary":
                files[file_id].blob_sha256 = str(op["blob_sha256"])
            if op.get("bytes_base64"):
                self.blobs[str(op["blob_sha256"])] = base64.b64decode(str(op["bytes_base64"]))
        updated.files = list(files.values())
        updated.revision += 1
        self.current = updated
        self.history[updated.revision] = updated.model_copy(deep=True)
        result = {"revision": updated.revision, "receipt_key": key}
        self.receipts[key] = result
        self.applied.append({"ops": ops, "expect": expect, "actor": actor})
        return result


def wire_saved_paper(service):
    from contextlib import nullcontext

    from rcp.paper.changes import PaperChanges

    live = FakePaperLive(
        SavedPaperSnapshot(
            revision=1,
            files=[
                SavedPaperFile(
                    file_id="paper-file", path="paper.md", kind="text", text="Saved paper text.\n"
                )
            ],
        )
    )
    service.paper_live = live
    service.paper_changes = PaperChanges(service.paper.store, live, lambda _: nullcontext())
    return live
