"""S3 integration seam: S1 supplies this saved-paper API; no published-file fallback.

PaperOp/expect field names below are the explicit adapter boundary pending S1.
``apply`` must atomically persist its receipt_key with the live mutation and return
an earlier receipt before checking preconditions when that key already exists.

Ops are dictionaries with ``kind`` text/create/rename/delete/binary and stable
``file_id``. Text uses start/end/text; create uses path/file_kind/text or
blob_sha256/bytes_base64; rename uses path; binary uses blob_sha256/bytes_base64.
Create can restore a deleted identity for Undo. Expect has optional tree revision
and a checks list: file_id/path/kind, region start/end/text, whole text/blob_sha256,
path_free and file_id_absent. All checks observe the pre-mutation tree. Definite
atomic precondition refusal must raise changes.OutdatedChange; uncertain failures
must not, because the prepared receipt must remain replayable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol, cast

from pydantic import BaseModel, Field


class SavedPaperFile(BaseModel):
    file_id: str
    path: str
    kind: Literal["text", "binary"]
    text: str | None = None
    blob_sha256: str | None = None


class SavedPaperSnapshot(BaseModel):
    revision: int
    files: list[SavedPaperFile] = Field(default_factory=list)


class PaperLive(Protocol):
    def snapshot(self, project_id: str, revision: int | None = None) -> SavedPaperSnapshot: ...

    def materialize(self, snapshot: SavedPaperSnapshot, dest: Path) -> None: ...

    def text_at(self, project_id: str, file_id: str, revision: int) -> str: ...

    def apply(
        self,
        project_id: str,
        ops: list[dict[str, object]],
        actor: str,
        expect: dict[str, object],
    ) -> object: ...


def get_paper_live(owner: object) -> PaperLive:
    """S1 wires ProjectService.paper_live; refuse until that owner is available."""
    live = getattr(owner, "paper_live", None)
    if live is None:
        raise ValueError("Saved paper service is unavailable")
    return cast(PaperLive, live)
