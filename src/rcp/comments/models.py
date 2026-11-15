from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rcp.artifacts import ARTIFACT_MEDIA_TYPES
from rcp.limits import COMMENT_POST_MAX_CHARS, COMMENT_THREAD_MAX_CHARS


class ArtifactSelectionRect(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def stays_inside_viewport(self) -> ArtifactSelectionRect:
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("artifact selection rectangle must stay inside its viewport")
        return self


class ArtifactViewport(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    width: int = Field(ge=1, le=32768)
    height: int = Field(ge=1, le=32768)


class CommentAnchorFields(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    comment: str = Field(default="", max_length=COMMENT_THREAD_MAX_CHARS)
    where: str | None = Field(default=None, max_length=256)
    drawn_version: str | None = None


class ArtifactTextSelection(CommentAnchorFields):
    kind: Literal["text"]
    text: str = Field(min_length=1, max_length=4096)
    surrounding_text: str = Field(default="", max_length=6144)


class ArtifactBoxElement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(min_length=1, max_length=512)
    label: str = Field(default="", max_length=256)
    text: str = Field(default="", max_length=512)
    region: ArtifactSelectionRect | None = None


class ArtifactBoxSelection(CommentAnchorFields):
    kind: Literal["box"]
    rect: ArtifactSelectionRect
    viewport: ArtifactViewport
    elements: list[ArtifactBoxElement] | None = Field(default=None, max_length=8)
    labels: str = Field(default="", max_length=4096)


class ArtifactWholeSelection(CommentAnchorFields):
    kind: Literal["whole"]


class ArtifactElementSelection(CommentAnchorFields):
    kind: Literal["element"]
    path: str = Field(min_length=1, max_length=512)
    label: str = Field(default="", max_length=256)
    text: str = Field(default="", max_length=4096)


class ArtifactPdfSelection(CommentAnchorFields):
    kind: Literal["pdf"]
    page: int = Field(ge=1)
    rect: ArtifactSelectionRect | None = None
    text: str = Field(default="", max_length=4096)


CommentAnchor = Annotated[
    ArtifactTextSelection
    | ArtifactBoxSelection
    | ArtifactWholeSelection
    | ArtifactElementSelection
    | ArtifactPdfSelection,
    Field(discriminator="kind"),
]


class CommentTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["artifact", "paper", "reply"]
    id: str = Field(min_length=1)


class CommentPostBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: str = Field(min_length=1, max_length=COMMENT_POST_MAX_CHARS)


class CreateCommentThread(CommentPostBody):
    target: CommentTarget
    anchor: CommentAnchor
    drawn_version: str | None = None


COMMENT_MEDIA_TYPES: dict[str, frozenset[str]] = {
    "whole": frozenset(ARTIFACT_MEDIA_TYPES.values()) - {"application/octet-stream"},
    "text": frozenset({"text/html", "text/markdown", "text/plain"}),
    "box": frozenset(
        {"text/html", "image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"}
    ),
    "element": frozenset({"text/html"}),
    "pdf": frozenset({"application/pdf"}),
}

COMMENT_TARGET_ANCHOR_KINDS: dict[str, frozenset[str]] = {
    "artifact": frozenset(COMMENT_MEDIA_TYPES),
    "paper": frozenset({"whole", "text", "element", "pdf", "box"}),
    "reply": frozenset({"whole", "text"}),
}
