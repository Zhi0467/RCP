from __future__ import annotations

import html
import importlib.resources
import json
from dataclasses import dataclass
from pathlib import PurePosixPath

from markdown_it import MarkdownIt

from rcp.artifacts import (
    AgentArtifactDescriptor,
    ArtifactMediaType,
    FrameAddon,
    artifact_view,
    classify_artifact_bytes,
    html_preview_document,
)
from rcp.escaped_lines import escaped_lines
from rcp.limits import (
    ARTIFACT_PREVIEW_MAX_BYTES,
    ARTIFACT_PREVIEW_MAX_LINES,
    LIVE_ARTIFACT_REFRESH_SECONDS,
)

ARTIFACT_TEXT_CSP = (
    "sandbox; default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'self'"
)


@dataclass(frozen=True)
class ViewerPanel:
    markup: str
    style: str
    script: str


# Team servers accept authenticated mutations only as JSON (the CSRF guard in
# rcp.api.app), so the viewer's bodiless POSTs still declare an empty JSON body.
VIEWER_MUTATION_INIT = {
    "method": "POST",
    "credentials": "same-origin",
    "headers": {"Content-Type": "application/json"},
    "body": "{}",
}
_VIEWER_MUTATION_INIT_JS = json.dumps(VIEWER_MUTATION_INIT)

_KEEP_SAVE_HANDLERS_JS = (
    # The shell defines `config` with keepUrl/saveUrl and its status element.
    "const keep=document.getElementById('keep');if(keep) keep.addEventListener('click',async()=>{keep.disabled=true;notice.textContent='';try{const response=await fetch(config.keepUrl,"
    + _VIEWER_MUTATION_INIT_JS
    + ");if(!response.ok)throw new Error('Keep failed');keep.remove();notice.textContent='Kept.';}catch(error){keep.disabled=false;notice.textContent=error instanceof Error?error.message:String(error);}});\n"
    "const save=document.getElementById('save');if(save) save.addEventListener('click',async()=>{save.disabled=true;notice.textContent='';try{const response=await fetch(config.saveUrl,"
    + _VIEWER_MUTATION_INIT_JS
    + ");if(!response.ok)throw new Error('Could not save the report. Try again.');const result=await response.json();notice.textContent=`Saved to ${result.path}`;}catch(error){notice.textContent=error instanceof Error?error.message:String(error);}finally{save.disabled=false;}});\n"
)


def artifact_viewer_document(
    descriptor: AgentArtifactDescriptor,
    *,
    content_url: str,
    state: str,
    keep_url: str | None = None,
    save_url: str | None = None,
    panel: ViewerPanel | None = None,
    live_url: str | None = None,
) -> tuple[str, str]:
    kind = artifact_view(descriptor.media_type)
    if kind in {"pdf", "file"}:
        raise ValueError("Artifact has no viewer")
    title = html.escape(descriptor.name, quote=True)
    url = html.escape(content_url, quote=True)
    preview = (
        f'<img id="previewImage" src="{url}" alt="{title}">'
        if kind == "image"
        else f'<iframe id="preview" sandbox="{"allow-scripts" if kind == "html" else ""}" '
        f'src="{url}" title="{title}"></iframe>'
    )
    keep = (
        '<button id="keep" type="button">Keep</button>'
        if keep_url and not descriptor.is_kept()
        else ""
    )
    save = '<button id="save" type="button">Save copy</button>' if save_url else ""
    scripts = []
    if keep or save:
        config = json.dumps({"keepUrl": keep_url, "saveUrl": save_url}).replace("<", "\\u003c")
        scripts.append(
            f"const config={config};const notice=document.getElementById('notice');\n"
            + _KEEP_SAVE_HANDLERS_JS
        )
    if panel and panel.script:
        scripts.append(panel.script)
    if live_url and kind == "html":
        scripts.append(
            "const liveUrl="
            + json.dumps(live_url).replace("<", "\\u003c")
            + ";\n"
            + f"const defaultLiveDelay={LIVE_ARTIFACT_REFRESH_SECONDS * 1000};\n"
            + importlib.resources.files("rcp").joinpath("artifact_live.js").read_text("utf-8")
        )
    # The RCP panel shows the name and state; the shell keeps a bar only for its actions.
    notice = "" if panel else '<span id="notice" role="status"></span>'
    header = (
        f'<header><span class="spacer"></span>{save}{keep}{notice}</header>\n'
        if save or keep or notice
        else ""
    )
    rows = "48px minmax(0,1fr)" if header else "minmax(0,1fr)"
    script_markup = "".join(f"<script>(()=>{{{script}}})();</script>" for script in scripts)
    document = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
:root{{--paper:#f4f1e8;--ink:#211f1a;--muted:#736f65;--rule:#c9c3b5;--accent:#a94f31;--panel:#fbfaf5}}
*{{box-sizing:border-box}}html,body{{margin:0;height:100%;background:var(--paper);color:var(--ink);font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}}
body{{display:grid;grid-template-rows:{rows}}}header{{display:flex;align-items:center;gap:12px;padding:0 16px;border-bottom:1px solid var(--rule);background:var(--panel)}}
.spacer{{flex:1}}
button{{border:1px solid var(--rule);background:transparent;color:var(--ink);padding:6px 10px;border-radius:2px;font:inherit;cursor:pointer}}button:disabled{{opacity:.45;cursor:default}}
main{{display:grid;min-height:0}}.canvas{{position:relative;min-width:0;min-height:0;background:white}}
iframe{{display:block;border:0;width:100%;height:100%}}.canvas>img{{display:block;width:100%;height:100%;object-fit:contain}}
{panel.style if panel else ""}</style></head><body>
{header}<main><div class="canvas">{preview}</div>{panel.markup if panel else ""}</main>{script_markup}</body></html>"""
    csp = "default-src 'none'; "
    if scripts:
        csp += "script-src 'unsafe-inline'; connect-src 'self'; "
    csp += "style-src 'unsafe-inline'; frame-src 'self'; img-src 'self' data: blob:; base-uri 'none'; form-action 'none'; object-src 'none'; frame-ancestors 'self'"
    return document, csp


def _preview_text(data: bytes) -> tuple[str, bool]:
    bounded = data[:ARTIFACT_PREVIEW_MAX_BYTES]
    truncated = len(bounded) < len(data)
    # The classifier already required strict UTF-8. A byte boundary may bisect its last character.
    text = bounded.decode("utf-8", errors="ignore" if truncated else "strict")
    lines = text.split("\n")
    if len(lines) > ARTIFACT_PREVIEW_MAX_LINES:
        text = "\n".join(lines[:ARTIFACT_PREVIEW_MAX_LINES])
        truncated = True
    return text, truncated


def _text_page(body: str, *, truncated: bool) -> tuple[str, str]:
    marker = (
        '<p id="truncated" role="status">Preview truncated. Download contains the whole file.</p>'
        if truncated
        else ""
    )
    return (
        '<!doctype html><html><head><meta charset="utf-8"><style>'
        "body{margin:24px;font:15px/1.5 system-ui;overflow-wrap:anywhere}"
        "pre{white-space:pre-wrap}.line{display:block;min-height:1.5em;padding-left:4.5em}"
        ".line::before{content:attr(id);display:inline-block;width:4em;margin-left:-4.5em;color:GrayText;user-select:none}"
        "</style></head><body>" + marker + body + "</body></html>",
        ARTIFACT_TEXT_CSP,
    )


def markdown_document(data: bytes) -> tuple[str, str]:
    text, truncated = _preview_text(data)
    renderer = MarkdownIt("commonmark", {"html": False})

    def link_open(tokens, index, options, env):
        return ""

    def link_close(tokens, index, options, env):
        depth = 1
        for token in reversed(tokens[:index]):
            if token.type == "link_close":
                depth += 1
            elif token.type == "link_open":
                depth -= 1
                if depth == 0:
                    return " (" + html.escape(token.attrGet("href") or "") + ")"
        return ""

    def image(tokens, index, options, env):
        token = tokens[index]
        return html.escape(token.content) + " (" + html.escape(token.attrGet("src") or "") + ")"

    renderer.renderer.rules.update(link_open=link_open, link_close=link_close, image=image)
    return _text_page(renderer.render(text), truncated=truncated)


def text_document(name: str, data: bytes) -> tuple[str, str]:
    text, truncated = _preview_text(data)
    if not truncated and PurePosixPath(name).suffix.lower() in {".json", ".ipynb"}:
        try:
            pretty = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except (ValueError, RecursionError):
            pass
        else:
            if (
                len(pretty.encode("utf-8")) <= ARTIFACT_PREVIEW_MAX_BYTES
                and len(pretty.split("\n")) <= ARTIFACT_PREVIEW_MAX_LINES
            ):
                text = pretty
    return _text_page("<pre><code>" + escaped_lines(text) + "</code></pre>", truncated=truncated)


def artifact_content(
    name: str, media_type: ArtifactMediaType, data: bytes, *, frame_addon: FrameAddon | None = None
) -> tuple[str | bytes, str, str]:
    """Render stored bytes through the single sandboxed artifact boundary."""
    if classify_artifact_bytes(name, data) != media_type:
        raise ValueError("Artifact media type changed")
    view = artifact_view(media_type)
    try:
        if view == "html":
            live_addon = live_frame_addon()
            document, csp = html_preview_document(
                data,
                frame_addon=FrameAddon(
                    frame_script=live_addon.frame_script
                    + (frame_addon.frame_script if frame_addon else ""),
                    wrapper_script=live_addon.wrapper_script
                    + (frame_addon.wrapper_script if frame_addon else ""),
                ),
            )
        elif view == "markdown":
            document, csp = markdown_document(data)
        elif view == "text":
            document, csp = text_document(name, data)
        elif view == "image":
            return data, media_type, "default-src 'none'; sandbox"
        else:
            raise ValueError("Artifact has no viewer")
    except Exception as exc:
        raise ValueError("Preview unavailable") from exc
    return document, "text/html", csp


def live_frame_addon() -> FrameAddon:
    """Relay only shell-provided data through the existing private preview channel."""
    return FrameAddon(
        frame_script="""
const dispatchLive=Function.prototype.call.bind(EventTarget.prototype.dispatchEvent);
const LiveMessage=MessageEvent;
listen(privatePort,'message',(event)=>{
  if(event.data?.kind==='rcp-live-data' && event.data.version===1)
    dispatchLive(window,new LiveMessage('message',{data:event.data}));
});
""",
        wrapper_script="""
let pendingLive=null;
channelReady.push(()=>{
  if(pendingLive) portPost(artifactPort,pendingLive);
  if(window.parent!==window) parentPost({type:'rcp-live-ready',version:1},'*');
});
listen(window,'message',(event)=>{
  if(window.parent===window || event.source!==window.parent) return;
  if(event.data?.kind!=='rcp-live-data' || event.data.version!==1) return;
  pendingLive=event.data;
  if(artifactPort) portPost(artifactPort,pendingLive);
});
""",
    )
