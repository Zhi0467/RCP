from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr


class PaperConflict(ValueError):
    """A guarded operation no longer describes the saved tree."""


class PaperPrecondition(PaperConflict):
    """An atomic live mutation definitely refused its saved-tree preconditions."""


class PaperLimit(ValueError):
    """A Paper resource budget would be exceeded."""


class PaperFile(BaseModel):
    file_id: str
    path: str
    kind: Literal["text", "binary"]
    text: str | None = None
    blob_sha256: str | None = None


class PaperSnapshot(BaseModel):
    revision: int
    files: list[PaperFile]
    # Captured bytes keep a snapshot materializable after SQLite retention runs.
    _blobs: dict[str, bytes] = PrivateAttr(default_factory=dict)


class PaperTreeFile(BaseModel):
    file_id: str
    path: str
    kind: Literal["text", "binary"]
    format: str
    size: int
    sha256: str


class PaperTree(BaseModel):
    revision: int
    saved_revision: int
    published_revision: int
    publish_state: Literal["published", "pending", "failed"]
    hold: str | None
    files: list[PaperTreeFile]


class PaperOp(BaseModel):
    """Offsets are Python Unicode character offsets in the CURRENT saved text."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["replace", "create", "rename", "delete", "binary"]
    file_id: str | None = None
    path: str | None = None
    text: str = ""
    start: int | None = None
    end: int | None = None
    data: bytes | None = None
    blob_sha256: str | None = None
    file_kind: Literal["text", "binary"] = "text"


class Receipt(BaseModel):
    revision: int
    receipt_key: str | None = None
    file_ids: list[str] = Field(default_factory=list)


def paper_path(value: str) -> str:
    if (
        not value
        or len(value.encode()) > 1024
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or PurePosixPath(value).is_absolute()
    ):
        raise ValueError("Invalid Paper path")
    return value


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_format(path: str, kind: str) -> str:
    extension = PurePosixPath(path).suffix.lower()
    return {
        ".md": "markdown",
        ".tex": "latex",
        ".typ": "typst",
        ".bib": "bibtex",
        ".png": "image",
        ".jpg": "image",
        ".jpeg": "image",
        ".gif": "image",
        ".webp": "image",
        ".svg": "image",
        ".pdf": "pdf",
    }.get(extension, "text" if kind == "text" else "other")
