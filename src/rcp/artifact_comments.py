from __future__ import annotations

import importlib.resources
import io
import json
import math
import re
from urllib.parse import quote

from PIL import Image, ImageOps

from rcp.artifact_renderer import ArtifactRendererUnavailable
from rcp.artifact_views import ViewerPanel
from rcp.artifacts import ARTIFACT_MEDIA_TYPES, FrameAddon
from rcp.limits import (
    ARTIFACT_CONTEXT_MAX_SELECTIONS,
    ARTIFACT_CROP_MAX_PIXELS,
    ARTIFACT_CROP_MAX_SIDE,
    ARTIFACT_VIEWER_STATE_REFRESH_MS,
    STEERING_MESSAGE_MAX_CHARS,
)
from rcp.web_assets import web_dist_path

COMMENTABLE_MEDIA_TYPES = frozenset(
    {"text/html", "image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"}
)


def supports_comments(media_type: str) -> bool:
    return media_type in set(ARTIFACT_MEDIA_TYPES.values()) - {
        "application/pdf",
        "application/octet-stream",
    }


# Raster artifacts a boxed selection is cropped from; SVG and HTML are read as source.
CROPPABLE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


def croppable_frame(data: bytes) -> tuple[Image.Image | None, bool]:
    """Decode a raster artifact once, as the viewer shows it, for its boxed regions.

    The frame is the first one, turned by the image's own orientation as a browser
    turns it. The flag says the image is animated, so a crop shows only that frame.
    An image over the pixel bound returns no frame: its boxes travel as positions.
    """

    try:
        with Image.open(io.BytesIO(data)) as image:
            columns, rows = image.size
            animated = getattr(image, "n_frames", 1) > 1
            if columns * rows > ARTIFACT_CROP_MAX_PIXELS:
                return None, animated
            image.seek(0)
            frame = ImageOps.exif_transpose(image).convert("RGBA")
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError("The image could not be decoded to crop a selection.") from exc
    return frame, animated


def crop_region(frame: Image.Image, *, x: float, y: float, width: float, height: float) -> bytes:
    """Cut one region, given as fractions of the frame, scaled to fit, as PNG."""

    columns, rows = frame.size
    left = min(columns - 1, math.floor(x * columns))
    top = min(rows - 1, math.floor(y * rows))
    right = max(left + 1, min(columns, math.ceil((x + width) * columns)))
    bottom = max(top + 1, min(rows, math.ceil((y + height) * rows)))
    # Resample the box straight into the bounded size; a full-size copy of a large
    # region per selection would cost memory the output never needs.
    scale = min(1.0, ARTIFACT_CROP_MAX_SIDE / max(right - left, bottom - top))
    size = (max(1, round((right - left) * scale)), max(1, round((bottom - top) * scale)))
    crop = frame.resize(size, Image.Resampling.LANCZOS, box=(left, top, right, bottom))
    output = io.BytesIO()
    crop.save(output, format="PNG")
    return output.getvalue()


def _selection_script() -> str:
    return importlib.resources.files("rcp").joinpath("artifact_selection.js").read_text("utf-8")


def _comment_panel_assets() -> tuple[str, str]:
    root = web_dist_path()
    try:
        script = (root / "comments.js").read_text(encoding="utf-8")
        style = (root / "comments.css").read_text(encoding="utf-8")
    except OSError as exc:
        raise ArtifactRendererUnavailable(
            "The comment viewer is unavailable. Rebuild the Web assets."
        ) from exc
    if not script.strip() or not style.strip():
        raise ArtifactRendererUnavailable("The comment viewer is empty. Rebuild the Web assets.")
    return re.sub(
        "</script", lambda match: "<\\/" + match.group(0)[2:], script, flags=re.IGNORECASE
    ), style


def selection_frame_addon() -> FrameAddon:
    return FrameAddon(
        frame_script=_selection_script()
        + """
let clearSelection=null;
let selectionActive=false;
listen(privatePort,'message',(event)=>{
  if(event.data?.kind==='rcp-artifact-selection-enable'){
    selectionActive=true;
    if(!clearSelection)
      clearSelection=installArtifactSelection(document,(selection)=>send({kind:'rcp-artifact-selection',selection}),()=>selectionActive);
  }
  if(event.data?.kind==='rcp-artifact-selection-disable'){
    selectionActive=false;
    clearSelection?.();
  }
  // A comment that closed takes its text selection with it; a drag over text still
  // highlighted would move the text instead of selecting.
  if(event.data?.kind==='rcp-artifact-selection-clear'){
    clearSelection?.();
    document.getSelection()?.removeAllRanges();
  }
});
""",
        wrapper_script="""let selectionEnabled=false;
channelListeners.push((value)=>{
    if(value.kind==='rcp-artifact-selection' && 'selection' in value && window.parent!==window){
      parentPost({type:'rcp-artifact-selection',version:1,selection:value.selection},'*');
      return;
    }
});
channelReady.push(()=>{if(selectionEnabled) portPost(artifactPort,{kind:'rcp-artifact-selection-enable'});});
listen(window,'message',(event)=>{
  if(window.parent===window || event.source!==window.parent) return;
  if(event.data?.type==='rcp-artifact-selection-enable'){
    selectionEnabled=true;
    if(artifactPort) portPost(artifactPort,{kind:'rcp-artifact-selection-enable'});
  }
  if(event.data?.type==='rcp-artifact-selection-disable'){
    selectionEnabled=false;
    if(artifactPort) portPost(artifactPort,{kind:'rcp-artifact-selection-disable'});
  }
  if(event.data?.type==='rcp-artifact-selection-clear' && artifactPort)
    portPost(artifactPort,{kind:'rcp-artifact-selection-clear'});
});
""",
    )


def comment_panel(config: dict[str, object]) -> ViewerPanel:
    base_url = (
        f"/api/projects/{quote(str(config['projectId']), safe='')}/artifacts/"
        f"{quote(str(config['artifactId']), safe='')}"
    )
    config = {
        **config,
        "maxSelections": ARTIFACT_CONTEXT_MAX_SELECTIONS,
        "maxChars": STEERING_MESSAGE_MAX_CHARS,
        "stateRefreshMs": ARTIFACT_VIEWER_STATE_REFRESH_MS,
        "selectionEnabled": config.get("mediaType") in COMMENTABLE_MEDIA_TYPES,
        "stateUrl": base_url + "/state",
        "commentsUrl": base_url + "/comments",
    }
    encoded = json.dumps(config, ensure_ascii=False).replace("<", "\\u003c")
    script, style = _comment_panel_assets()
    return ViewerPanel(
        markup='<aside id="comments-root" aria-label="Comments"></aside>',
        style=style,
        script=script + "\nRcpComments.mountViewer(" + encoded + ");",
    )
