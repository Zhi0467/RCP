from __future__ import annotations

import importlib.resources
import io
import json
import math
from urllib.parse import quote

from PIL import Image, ImageOps

from rcp.artifact_views import ViewerPanel
from rcp.artifacts import ARTIFACT_MEDIA_TYPES, FrameAddon
from rcp.limits import (
    ARTIFACT_CONTEXT_MAX_SELECTIONS,
    ARTIFACT_CROP_MAX_PIXELS,
    ARTIFACT_CROP_MAX_SIDE,
    ARTIFACT_VIEWER_STATE_REFRESH_MS,
)

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


def _comment_panel_script() -> str:
    return importlib.resources.files("rcp").joinpath("artifact_comment_panel.js").read_text("utf-8")


def selection_frame_addon() -> FrameAddon:
    return FrameAddon(
        frame_script=_selection_script()
        + """
let clearSelection=null;
listen(privatePort,'message',(event)=>{
  if(event.data?.kind==='rcp-artifact-selection-enable' && !clearSelection)
    clearSelection=installArtifactSelection(document,(selection)=>send({kind:'rcp-artifact-selection',selection}));
  if(event.data?.kind==='rcp-artifact-selection-clear') clearSelection?.();
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
        "stateRefreshMs": ARTIFACT_VIEWER_STATE_REFRESH_MS,
        "selectionEnabled": config.get("mediaType") in COMMENTABLE_MEDIA_TYPES,
        "stateUrl": base_url + "/state",
        "commentsUrl": base_url + "/comments",
    }
    hint_hidden = "" if config["selectionEnabled"] else " hidden"
    encoded = json.dumps(config, ensure_ascii=False).replace("<", "\\u003c")
    # One floating window: the composer for the comment being written, and a folded
    # tray of added comments. Every comment is the same object and one send route.
    return ViewerPanel(
        markup=f'<aside class="comment-float" aria-label="Comments"><section id="composer" class="composer" role="dialog" aria-label="Comment" hidden><div class="excerpt"></div><textarea id="message" maxlength="2048" placeholder="Comment" aria-label="Comment"></textarea><div class="actions"><button data-cancel type="button">Cancel</button><button data-confirm id="queue" type="button" disabled>Add comment</button><button id="editNow" class="primary" type="button" disabled>Edit now</button></div></section><details id="tray" class="tray"><summary>Comments <span id="count">0</span></summary><p id="hint" class="empty"{hint_hidden}>Select text or drag an area to comment.</p><div id="items"></div><div class="actions"><button id="general" type="button">Comment on the whole artifact</button><button id="send" class="primary" type="button" disabled>Send to original chat</button></div></details><div id="notice" class="notice" role="status"></div></aside>',
        style="""main{grid-template-columns:minmax(0,1fr)}#boxLayer{position:absolute;cursor:crosshair}
.comment-float{position:fixed;right:16px;bottom:16px;z-index:5;display:grid;gap:8px;width:min(340px,calc(100vw - 32px));max-height:calc(100vh - 32px)}
.composer,.tray{background:var(--panel);border:1px solid var(--rule);border-radius:8px;box-shadow:0 10px 28px rgb(0 0 0/.18);padding:12px;overflow:auto}
.tray{padding:8px 12px}.tray summary{cursor:pointer;font:600 12px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.tray[open] summary{margin-bottom:8px}.actions{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:6px;margin-top:10px}
.primary{background:var(--accent);color:white;border-color:var(--accent)}.primary:disabled{opacity:.5}
.empty{margin:0 0 8px;color:var(--muted);font-family:Georgia,serif;font-style:italic;font-size:13px}
.selection{border-top:1px solid var(--rule);padding:8px 0}.selection b{display:block;margin-bottom:4px;color:var(--accent);font-size:11px;text-transform:uppercase;letter-spacing:.08em}
.selection p{margin:4px 0 0;font:13px/1.4 Georgia,serif}.selection .remove{float:right;padding:2px 6px;font-size:11px}
.excerpt{max-height:72px;overflow:auto;font-family:Georgia,serif;font-size:13px;color:var(--muted)}
textarea{width:100%;min-height:72px;margin-top:8px;resize:vertical;border:1px solid var(--rule);background:white;padding:8px;color:var(--ink);font:13px/1.4 Georgia,serif;box-sizing:border-box}
.notice{color:var(--accent);font-size:12px;text-align:right}.notice:empty{display:none}
""",
        script=_selection_script() + "\nconst config=" + encoded + ";\n" + _comment_panel_script(),
    )
