from __future__ import annotations

import html
import importlib.resources
import io
import json
import math
from urllib.parse import quote, urlencode

from PIL import Image, ImageOps

from rcp.artifact_views import ViewerPanel
from rcp.artifacts import ARTIFACT_MEDIA_TYPES, FrameAddon
from rcp.limits import (
    ARTIFACT_CHAT_OPEN_TIMEOUT_MS,
    ARTIFACT_CONTEXT_MAX_SELECTIONS,
    ARTIFACT_CROP_MAX_PIXELS,
    ARTIFACT_CROP_MAX_SIDE,
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
    config = {
        "chatAvailable": True,
        "chatOpenTimeoutMs": ARTIFACT_CHAT_OPEN_TIMEOUT_MS,
        "maxSelections": ARTIFACT_CONTEXT_MAX_SELECTIONS,
        **config,
    }
    query = {"view": "chats", "chat": str(config["chatId"])}
    if config.get("branchId") is not None:
        query["branch_id"] = str(config["branchId"])
    chat_href = f"/#/projects/{quote(str(config['projectId']), safe='')}?{urlencode(query)}"
    encoded = json.dumps(config, ensure_ascii=False).replace("<", "\\u003c")
    return ViewerPanel(
        markup=f'<aside><h2>Selections</h2><section id="pending" aria-label="Confirm selection" hidden><div class="excerpt"></div><button data-confirm type="button">Comment</button> <button data-cancel type="button">Cancel</button></section><div id="empty" class="empty">Select text or drag an area, then choose Comment.</div><div id="items"></div><button id="add" class="add" type="button" disabled>Add to chat</button><a id="open-chat" class="open-chat" href="{html.escape(chat_href, quote=True)}" hidden>Open chat</a><div id="notice" class="notice" role="status"></div></aside>',
        style="""main{grid-template-columns:minmax(0,1fr) 300px}.canvas{border-right:1px solid var(--rule)}#boxLayer{position:absolute;cursor:crosshair}aside{padding:14px;overflow:auto;background:var(--panel)}
aside h2{margin:0 0 12px;font:600 12px/1.2 ui-monospace,SFMono-Regular,Menlo,monospace;text-transform:uppercase;letter-spacing:.1em;color:var(--muted)}#pending{border:1px solid var(--accent);padding:12px;margin-bottom:12px}#pending button{margin-top:10px}#pending [data-confirm]{background:var(--accent);color:white;border-color:var(--accent)}
.empty{color:var(--muted);font-family:Georgia,serif;font-style:italic}#pending:not([hidden]) + #empty{display:none}.selection{border-top:1px solid var(--rule);padding:12px 0}
.selection b{display:block;margin-bottom:5px;color:var(--accent);font-size:11px;text-transform:uppercase;letter-spacing:.08em}
.selection .remove{float:right;padding:2px 6px;font-size:11px}
.excerpt{max-height:90px;overflow:auto;font-family:Georgia,serif;font-size:13px}
textarea{width:100%;min-height:62px;margin-top:8px;resize:vertical;border:1px solid var(--rule);background:white;padding:8px;color:var(--ink);font:13px/1.4 Georgia,serif}
.add{width:100%;margin-top:12px;background:var(--ink);color:var(--paper);border-color:var(--ink)}.add:hover{background:var(--accent);color:white}
.open-chat{display:block;margin-top:10px;padding:8px;text-align:center;border:1px solid var(--accent);color:var(--accent);text-decoration:none}.open-chat[hidden]{display:none}
.notice{margin-top:10px;color:var(--accent);font-size:12px}@media(max-width:760px){main{grid-template-columns:1fr;grid-template-rows:minmax(360px,1fr) auto}.canvas{border-right:0;border-bottom:1px solid var(--rule)}aside{max-height:42vh}}
""",
        script=_selection_script() + "\nconst config=" + encoded + ";\n" + _comment_panel_script(),
    )
