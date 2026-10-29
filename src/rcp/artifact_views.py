from __future__ import annotations

import html
import importlib.resources
import json
import secrets
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from markdown_it import MarkdownIt

from rcp.artifact_theme import (
    ArtifactColorMode,
    ArtifactTheme,
    artifact_theme_css,
    shell_theme_css,
)
from rcp.artifacts import (
    ARTIFACT_ERROR_VALIDATION_JS,
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
    LIVE_ARTIFACT_MESSAGE_MAX_BYTES,
    LIVE_ARTIFACT_REFRESH_SECONDS,
)

ARTIFACT_TEXT_CSP = (
    "sandbox; default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'self'"
)


ViewerPresentation = Literal["panel", "inline"]


@dataclass(frozen=True)
class ViewerPanel:
    markup: str
    style: str
    script: str


# The shell paints with the app's own theme tokens, in both modes. Aqua is the
# app's default theme, so a shell that cannot learn the choice starts there.
_THEME_TOKENS_CSS = shell_theme_css()

# Mirror the containing app's theme and mode, live; a shell opened on its own reads
# the remembered appearance. Same-origin reads only: the app frames its own shell.
_THEME_SYNC_JS = """const root=document.documentElement;
const dark=window.matchMedia?.('(prefers-color-scheme: dark)');
function applyTheme(){
  let theme=null,scheme=null;
  try{const app=window.parent!==window?window.parent.document.documentElement:null;
    if(app){theme=app.dataset.theme||null;scheme=app.dataset.colorMode||null;}}catch{}
  if(!theme){try{const saved=JSON.parse(localStorage.getItem('rcp:appearance')||'null');
    theme=saved?.theme||null;scheme=saved?.mode||null;}catch{}}
  root.dataset.theme=theme==='classic'?'classic':'aqua';
  root.dataset.colorMode=['dark','light'].includes(scheme)?scheme:dark?.matches?'dark':'light';
}
applyTheme();
dark?.addEventListener?.('change',applyTheme);
try{if(window.parent!==window)new MutationObserver(applyTheme).observe(window.parent.document.documentElement,{attributes:true,attributeFilter:['data-theme','data-color-mode']});}catch{}"""


@dataclass(frozen=True)
class InlineAppearance:
    """How a frame shown inside a reply paints: the reply's own theme and mode."""

    theme: ArtifactTheme
    mode: ArtifactColorMode


def inline_frame_addon(appearance: InlineAppearance) -> FrameAddon:
    """Paint the page with the reply's theme; its frame's height is fixed by the chat."""

    return FrameAddon(
        frame_style=artifact_theme_css(appearance.theme, appearance.mode),
        wrapper_style=f":root{{color-scheme:{appearance.mode}}}",
        frame_script="",
        wrapper_script="",
    )


def _combined_addon(*addons: FrameAddon | None) -> FrameAddon:
    present = [addon for addon in addons if addon]
    return FrameAddon(
        frame_script="".join(addon.frame_script for addon in present),
        wrapper_script="".join(addon.wrapper_script for addon in present),
        frame_style="".join(addon.frame_style for addon in present),
        wrapper_style="".join(addon.wrapper_style for addon in present),
    )


def _error_content_url(content_url: str, channel: str) -> str:
    url = urlsplit(content_url)
    query = dict(parse_qsl(url.query))
    query["error_channel"] = channel
    return urlunsplit(url._replace(query=urlencode(query)))


def _error_shell_script(channel: str, *, inline: bool) -> str:
    # A fresh channel on each content load rejects queued events from old bytes,
    # even when the browser reuses the iframe's WindowProxy.
    return (
        ARTIFACT_ERROR_VALIDATION_JS
        + "let errorChannel="
        + json.dumps(channel)
        + ";\n"
        + """
const errorFrame=document.getElementById('preview');
const errorNotice=document.getElementById('pageError');
const errorMessage=document.getElementById('pageErrorMessage');
const clearPageError=()=>{
  if(errorNotice) errorNotice.hidden=true;
  if(errorMessage) errorMessage.textContent='';
  CLEAR_INLINE
};
window.addEventListener('message',(event)=>{
  const value=event.data;
  if(!errorFrame || event.source!==errorFrame.contentWindow ||
     !value || typeof value!=='object' || value.channel!==errorChannel) return;
  if(value.kind==='rcp-artifact-error-clear' && Object.keys(value).length===2) {
    clearPageError(); return;
  }
  if(Object.keys(value).length!==4) return;
  const summary={kind:value.kind,message:value.message,count:value.count};
  if(!validArtifactError(summary)) return;
  if(errorMessage) errorMessage.textContent='This page hit an error: '+summary.message;
  if(errorNotice) {errorNotice.hidden=false;errorNotice.dataset.count=String(summary.count);}
  FORWARD_INLINE
});
""".replace(
            "CLEAR_INLINE",
            "window.parent.postMessage({kind:'rcp-artifact-error-clear'},location.origin);"
            if inline
            else "",
        ).replace(
            "FORWARD_INLINE",
            "window.parent.postMessage(summary,location.origin);" if inline else "",
        )
    )


_VIDEO_PREVIEW = (
    '<div id="videoPreview"><button id="videoPlay" type="button" aria-label="Play video">'
    '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" aria-hidden="true"><path d="M8 5l11 7-11 7z"/></svg></button></div>'
)
_VIDEO_STYLE = """
#videoPreview{display:grid;place-items:center;width:100%;max-width:100%;min-height:160px;aspect-ratio:16/9;border:1px solid var(--rule);border-radius:var(--radius);overflow:hidden}
#videoPreview.failed{border-color:var(--danger,#b42318)}
#videoPlay{display:grid;place-items:center;padding:12px;color:var(--ink);background:transparent;border:0;cursor:pointer}
#videoPreview video{display:block;width:100%;max-width:100%;max-height:100%;object-fit:contain}
"""


def _video_script(content_url: str) -> str:
    config = json.dumps(content_url).replace("<", "\\u003c")
    return f"const videoContentUrl={config};\n" + importlib.resources.files("rcp").joinpath(
        "artifact_video.js"
    ).read_text("utf-8")


def artifact_viewer_document(
    descriptor: AgentArtifactDescriptor,
    *,
    content_url: str,
    state: str,
    panel: ViewerPanel | None = None,
    live_url: str | None = None,
    presentation: ViewerPresentation = "panel",
    selectable: bool = False,
) -> tuple[str, str]:
    kind = artifact_view(descriptor.media_type)
    if kind in {"pdf", "file"}:
        raise ValueError("Artifact has no viewer")
    if presentation == "inline":
        return inline_viewer_document(
            descriptor.name,
            kind,
            content_url=content_url,
            live=(live_url, descriptor.artifact_id) if live_url else None,
            selectable=selectable,
        )
    error_channel = secrets.token_hex(16)
    if kind == "html":
        content_url = _error_content_url(content_url, error_channel)
    title = html.escape(descriptor.name, quote=True)
    url = html.escape(content_url, quote=True)
    preview = (
        f'<img id="previewImage" src="{url}" alt="{title}">'
        if kind == "image"
        else f'<iframe id="preview" sandbox="{"allow-scripts" if kind == "html" else ""}" '
        f'src="{url}" title="{title}"></iframe>'
    )
    if kind == "video":
        preview = _VIDEO_PREVIEW
    scripts: list[str] = [_video_script(content_url)] if kind == "video" else []
    if panel and panel.script:
        scripts.append(panel.script)
    if live_url and kind == "html":
        scripts.append(
            "const liveUrl="
            + json.dumps(live_url).replace("<", "\\u003c")
            + ";\n"
            + "const liveArtifactId="
            + json.dumps(descriptor.artifact_id)
            + ";\n"
            + f"const defaultLiveDelay={LIVE_ARTIFACT_REFRESH_SECONDS * 1000};\n"
            + f"const liveMessageMaxBytes={LIVE_ARTIFACT_MESSAGE_MAX_BYTES};\n"
            + importlib.resources.files("rcp").joinpath("artifact_live.js").read_text("utf-8")
        )
    page_error = (
        '<div id="pageError" hidden role="status"><span id="pageErrorMessage"></span>'
        + (' <button id="askFix" type="button">Ask to fix</button>' if panel else "")
        + "</div>"
        if kind == "html"
        else ""
    )
    # Comments, live data, and video fetches call RCP.
    connects = bool(scripts)
    if kind == "html":
        scripts.insert(0, _error_shell_script(error_channel, inline=False))
    script_markup = "".join(f"<script>(()=>{{{script}}})();</script>" for script in scripts)
    document = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><script>(()=>{{{_THEME_SYNC_JS}}})();</script><style>
{_THEME_TOKENS_CSS}
*{{box-sizing:border-box}}html,body{{margin:0;height:100%;background:var(--paper);color:var(--ink);font:14px/1.45 var(--ui)}}
body{{display:grid;grid-template-rows:minmax(0,1fr)}}
button{{border:1px solid var(--rule);background:transparent;color:var(--ink);padding:6px 10px;border-radius:var(--radius);font:inherit;cursor:pointer;box-shadow:var(--raised)}}button:disabled{{opacity:.45;cursor:default}}
main{{display:grid;grid-template-rows:minmax(0,1fr) auto;min-height:0}}
#pageError{{padding:6px 72px 6px 12px;min-width:0}}#pageError:not([hidden]){{display:flex;align-items:center;gap:8px}}
#pageErrorMessage{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}#askFix{{flex-shrink:0}}.canvas{{position:relative;min-width:0;min-height:0;background:white}}
iframe{{display:block;border:0;width:100%;height:100%}}.canvas>img{{display:block;width:100%;height:100%;object-fit:contain}}
{_VIDEO_STYLE if kind == "video" else ""}
{panel.style if panel else ""}</style></head><body>
<main><div class="canvas">{preview}</div>{page_error}{panel.markup if panel else ""}</main>{script_markup}</body></html>"""
    csp = "default-src 'none'; script-src 'unsafe-inline'; "
    if connects:
        csp += "connect-src 'self'; "
    csp += "style-src 'unsafe-inline'; frame-src 'self'; img-src 'self' data: blob:; base-uri 'none'; form-action 'none'; object-src 'none'; frame-ancestors 'self'"
    if kind == "video":
        csp += "; media-src blob:"
    return document, csp


def inline_viewer_document(
    name: str,
    kind: str,
    *,
    content_url: str,
    live: tuple[str, str] | None,
    selectable: bool,
) -> tuple[str, str]:
    """The same shell inside a reply: no chrome, transparent, filling its fixed frame.

    The chat owns the artifact's actions around it. Agent HTML keeps its unchanged
    opaque sandbox one frame further in; this shell adds no capability to it.
    `live` is the stored artifact's `(live_url, artifact_id)`; Paper previews have none.
    """

    error_channel = secrets.token_hex(16)
    if kind == "html":
        content_url = _error_content_url(content_url, error_channel)
    title = html.escape(name, quote=True)
    url = html.escape(content_url, quote=True)
    preview = (
        f'<img id="previewImage" src="{url}" alt="{title}">'
        if kind == "image"
        else f'<iframe id="preview" sandbox="allow-scripts" title="{title}"></iframe>'
    )
    if kind == "video":
        preview = _VIDEO_PREVIEW
    config = json.dumps(
        {
            "contentUrl": content_url,
            "selectable": selectable,
        }
    ).replace("<", "\\u003c")
    resources = importlib.resources.files("rcp")
    scripts = [
        _error_shell_script(error_channel, inline=True)
        + f"const inlineConfig={config};\n"
        + resources.joinpath("artifact_selection.js").read_text("utf-8")
        + "\n"
        + resources.joinpath("artifact_inline.js").read_text("utf-8")
    ]
    if kind == "video":
        scripts.append(_video_script(content_url))
    if live and kind == "html":
        live_url, artifact_id = live
        scripts.append(
            "const liveUrl="
            + json.dumps(live_url).replace("<", "\\u003c")
            + ";\n"
            + "const liveArtifactId="
            + json.dumps(artifact_id)
            + ";\n"
            + f"const defaultLiveDelay={LIVE_ARTIFACT_REFRESH_SECONDS * 1000};\n"
            + f"const liveMessageMaxBytes={LIVE_ARTIFACT_MESSAGE_MAX_BYTES};\n"
            + resources.joinpath("artifact_live.js").read_text("utf-8")
        )
    script_markup = "".join(f"<script>(()=>{{{script}}})();</script>" for script in scripts)
    document = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><script>(()=>{{{_THEME_SYNC_JS}}})();</script><style>
{_THEME_TOKENS_CSS}
*{{box-sizing:border-box}}html,body{{margin:0;background:transparent;color:var(--ink);font:15px/1.5 var(--ui)}}
html,body,.canvas{{height:100%}}.canvas{{position:relative}}iframe{{display:block;border:0;width:100%;height:100%;background:transparent}}
#previewImage{{display:block;max-width:100%;max-height:100vh;height:auto;border-radius:var(--radius)}}
body:has(#previewImage){{display:flex;align-items:center;justify-content:center}}.canvas:has(#previewImage){{display:inline-block;max-width:100%;height:auto}}#boxLayer{{position:absolute;inset:0;cursor:crosshair}}
{_VIDEO_STYLE + "#videoPreview{max-height:100vh}" if kind == "video" else ""}
</style></head><body><main class="canvas">{preview}</main>{script_markup}</body></html>"""
    csp = (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "frame-src 'self'; img-src 'self' data: blob:; base-uri 'none'; form-action 'none'; "
        "object-src 'none'; frame-ancestors 'self'"
    )
    if live and kind == "html":
        csp += "; connect-src 'self'"
    if kind == "video":
        csp += "; media-src blob:; connect-src 'self'"
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


def _text_page(
    body: str, *, truncated: bool, inline: InlineAppearance | None = None
) -> tuple[str, str]:
    marker = (
        '<p id="truncated" role="status">Preview truncated. Download contains the whole file.</p>'
        if truncated
        else ""
    )
    if inline is None:
        return (
            '<!doctype html><html><head><meta charset="utf-8"><style>'
            "body{margin:24px;font:15px/1.5 system-ui;overflow-wrap:anywhere}"
            "pre{white-space:pre-wrap}table{border-collapse:collapse;display:block;overflow-x:auto}"
            "th,td{border:1px solid GrayText;padding:4px 8px;text-align:left;vertical-align:top}"
            ".line{display:block;min-height:1.5em;padding-left:4.5em}"
            ".line::before{content:attr(id);display:inline-block;width:4em;margin-left:-4.5em;color:GrayText;user-select:none}"
            "</style></head><body>" + marker + body + "</body></html>",
            ARTIFACT_TEXT_CSP,
        )
    # The page holds only RCP-rendered, escaped text and runs no script at all.
    return (
        '<!doctype html><html><head><meta charset="utf-8"><style>'
        + artifact_theme_css(inline.theme, inline.mode)
        + "html{background:transparent}body{overflow-wrap:anywhere}"
        "body>:first-child{margin-top:0}body>:last-child{margin-bottom:0}"
        "pre{margin:0;padding:12px 14px;background:var(--rcp-field);border:1px solid var(--rcp-rule);"
        "border-radius:var(--rcp-radius);font:13px/1.55 var(--rcp-mono);white-space:pre-wrap}"
        "code{font-family:var(--rcp-mono)}a{color:var(--rcp-accent)}"
        "table{border-collapse:collapse;display:block;overflow-x:auto}"
        "th,td{border:1px solid var(--rcp-rule);padding:4px 8px;text-align:left;vertical-align:top}"
        ".line{display:block;min-height:1.55em;padding-left:4.5em}"
        ".line::before{content:attr(id);display:inline-block;width:4em;margin-left:-4.5em;"
        "color:var(--rcp-muted);user-select:none}"
        "#truncated{color:var(--rcp-muted);font-size:13px}"
        "</style></head><body>" + marker + body + "</body></html>",
        ARTIFACT_TEXT_CSP,
    )


def markdown_document(data: bytes, inline: InlineAppearance | None = None) -> tuple[str, str]:
    text, truncated = _preview_text(data)
    renderer = MarkdownIt("commonmark", {"html": False}).enable("table")

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
    return _text_page(renderer.render(text), truncated=truncated, inline=inline)


def text_document(
    name: str, data: bytes, inline: InlineAppearance | None = None
) -> tuple[str, str]:
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
    return _text_page(
        "<pre><code>" + escaped_lines(text) + "</code></pre>", truncated=truncated, inline=inline
    )


def artifact_content(
    name: str,
    media_type: ArtifactMediaType,
    data: bytes,
    *,
    frame_addon: FrameAddon | None = None,
    inline: InlineAppearance | None = None,
) -> tuple[str | bytes, str, str]:
    """Render stored bytes through the single sandboxed artifact boundary.

    `inline` renders the same bytes for a frame inside a reply, painted with the
    reply's theme. Nothing else differs.
    """
    if classify_artifact_bytes(name, data) != media_type:
        raise ValueError("Artifact media type changed")
    view = artifact_view(media_type)
    try:
        if view == "html":
            document, csp = html_preview_document(
                data,
                frame_addon=_combined_addon(
                    live_frame_addon(),
                    frame_addon,
                    inline_frame_addon(inline) if inline else None,
                ),
            )
        elif view == "markdown":
            document, csp = markdown_document(data, inline)
        elif view == "text":
            document, csp = text_document(name, data, inline)
        elif view in {"image", "video"}:
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
// Pages may check event.source===parent, as for any message from their embedder.
const liveSource=window.parent;
listen(privatePort,'message',(event)=>{
  if(event.data?.kind==='rcp-live-data' && event.data.version===1)
    dispatchLive(window,new LiveMessage('message',{data:event.data,source:liveSource}));
});
""",
        wrapper_script="""
let pendingLive=null;
let deliveredGraphIds=new Set();
listen(window,'message',(event)=>{
  const frame=artifact();
  const value=event.data;
  if(!frame || event.source!==frame.contentWindow || value?.type!=='rcp-open-node' ||
     value.version!==1 || typeof value.id!=='string' || !deliveredGraphIds.has(value.id)) return;
  parentPost({type:'rcp-open-node',version:1,id:value.id},'*');
});
channelReady.push(()=>{
  if(pendingLive) portPost(artifactPort,pendingLive);
  if(window.parent!==window) parentPost({type:'rcp-live-ready',version:1},'*');
});
listen(window,'message',(event)=>{
  if(window.parent===window || event.source!==window.parent) return;
  if(event.data?.kind!=='rcp-live-data' || event.data.version!==1) return;
  const graph=event.data.snapshots?.find(snapshot=>snapshot.kind==='graph');
  if(graph && !graph.error && !graph.unchanged && Array.isArray(graph.nodes))
    deliveredGraphIds=new Set(graph.nodes.map(node=>node.id));
  pendingLive=event.data;
  if(artifactPort) portPost(artifactPort,pendingLive);
});
""",
    )
