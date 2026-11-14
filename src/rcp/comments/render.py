from __future__ import annotations

import re
from typing import Any

from pydantic import TypeAdapter

from rcp.comments.models import CommentAnchor


def render_anchor(anchor: CommentAnchor | dict[str, Any]) -> str:
    selection = TypeAdapter(CommentAnchor).validate_python(anchor)
    lines: list[str] = []
    if selection.where:
        lines.append(f"Location: {selection.where}")
    if selection.drawn_version:
        lines.append(f"Drawn version: {selection.drawn_version}")
    if selection.kind == "whole":
        lines.append("Whole target")
    elif selection.kind == "text":
        lines.append(f"Quote: {selection.text}")
        if selection.surrounding_text:
            lines.append(f"Context: {selection.surrounding_text}")
    elif selection.kind == "element":
        lines.append(f"Element: {selection.path}")
        if selection.label:
            lines.append(f"Label: {selection.label}")
        if selection.text:
            lines.append(f"Quote: {selection.text}")
    elif selection.kind == "pdf":
        lines.append(f"PDF page: {selection.page}")
        if selection.rect is not None:
            rect = selection.rect
            lines.append(
                f"Box: x={rect.x:g}, y={rect.y:g}, width={rect.width:g}, height={rect.height:g}"
            )
        if selection.text:
            lines.append(f"Quote: {selection.text}")
    else:
        rect = selection.rect
        lines.append(
            f"Box: x={rect.x:g}, y={rect.y:g}, width={rect.width:g}, height={rect.height:g}"
        )
        lines.append(f"Viewport: {selection.viewport.width} x {selection.viewport.height}")
        for element in selection.elements or []:
            lines.append(f"Element: {element.path} {element.label}".rstrip())
            if element.region is not None:
                region = element.region
                lines.append(
                    f"Element region: x={region.x:g}, y={region.y:g}, "
                    f"width={region.width:g}, height={region.height:g}"
                )
            if element.text:
                lines.append(f"Quote: {element.text}")
        if selection.labels:
            lines.append(f"Nearby text: {selection.labels}")
    return "\n".join(lines)


def render_thread(thread: dict[str, Any], *, until: str | None = None) -> str:
    anchor = TypeAdapter(CommentAnchor).validate_python(thread["anchor"])
    if thread.get("drawn_version"):
        anchor = anchor.model_copy(update={"drawn_version": thread["drawn_version"]})
    lines = [render_anchor(anchor)]
    for post in thread["posts"]:
        if until is not None and post["created_at"] > until:
            continue
        mentions = {item["id"]: item["display_name"] for item in post.get("mentions", [])}
        text = re.sub(
            r"<@member:([^<>\s]+)>",
            lambda match, names=mentions: f"@{names[match[1]]}" if match[1] in names else match[0],
            post["text"],
        )
        author = post["author"]["display_name"]
        lines.append(f"{author}: {text}")
    return "\n".join(lines)
