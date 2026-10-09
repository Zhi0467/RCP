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
    ARTIFACT_INLINE_INITIAL_HEIGHT_PX,
    ARTIFACT_INLINE_MAX_HEIGHT_PX,
    ARTIFACT_PREVIEW_MAX_BYTES,
    ARTIFACT_PREVIEW_MAX_LINES,
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


# Team servers accept authenticated mutations only as JSON (the CSRF guard in
# rcp.api.app), so the viewer's bodiless POSTs still declare an empty JSON body.
VIEWER_MUTATION_INIT = {
    "method": "POST",
    "credentials": "same-origin",
    "headers": {"Content-Type": "application/json"},
    "body": "{}",
}
_VIEWER_MUTATION_INIT_JS = json.dumps(VIEWER_MUTATION_INIT)

_KEEP_HANDLER_JS = (
    # The shell defines `config` with keepUrl and its status element.
    "const keep=document.getElementById('keep');if(keep) keep.addEventListener('click',async()=>{keep.disabled=true;notice.textContent='';try{const response=await fetch(config.keepUrl,"
    + _VIEWER_MUTATION_INIT_JS
    + ");if(!response.ok)throw new Error('Keep failed');keep.remove();notice.textContent='Kept.';}catch(error){keep.disabled=false;notice.textContent=error instanceof Error?error.message:String(error);}});\n"
)


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


# A page sized by the viewport would measure as tall as its own frame and never
# settle; inside a reply the page's content decides the frame's height instead.
_INLINE_HEIGHT_CSS = "html,body{height:auto!important;min-height:0!important}"


def inline_frame_addon(appearance: InlineAppearance) -> FrameAddon:
    """Report the page's content height and paint it with the reply's theme.

    The bounded height is relayed by RCP's wrapper over the private channel.
    """

    return FrameAddon(
        frame_style=artifact_theme_css(appearance.theme, appearance.mode) + _INLINE_HEIGHT_CSS,
        wrapper_style=f":root{{color-scheme:{appearance.mode}}}",
        frame_script="""
const sizeRoot=document.documentElement;
const measureSize=Function.prototype.call.bind(Element.prototype.getBoundingClientRect);
const SizeObserver=ResizeObserver;
let reportedSize=-1;
const reportSize=()=>{
  const height=Math.ceil(measureSize(sizeRoot).height);
  if(height===reportedSize) return;
  reportedSize=height;
  send({kind:'rcp-artifact-size',height});
};
new SizeObserver(reportSize).observe(sizeRoot);
""",
        wrapper_script=f"""
channelListeners.push((value)=>{{
  if(value.kind!=='rcp-artifact-size' || typeof value.height!=='number' ||
     !Number.isFinite(value.height) || window.parent===window) return;
  parentPost({{type:'rcp-artifact-size',version:1,
    height:Math.max(0,Math.min({ARTIFACT_INLINE_MAX_HEIGHT_PX * 4},Math.ceil(value.height)))}},'*');
}});
""",
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


def artifact_viewer_document(
    descriptor: AgentArtifactDescriptor,
    *,
    content_url: str,
    state: str,
    keep_url: str | None = None,
    panel: ViewerPanel | None = None,
    live_url: str | None = None,
    presentation: ViewerPresentation = "panel",
    selectable: bool = False,
) -> tuple[str, str]:
    kind = artifact_view(descriptor.media_type)
    if kind in {"pdf", "file"}:
        raise ValueError("Artifact has no viewer")
    if presentation == "inline":
        return _inline_viewer_document(
            descriptor, kind, content_url=content_url, live_url=live_url, selectable=selectable
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
    keep = (
        '<button id="keep" type="button">Keep</button>'
        if keep_url and not descriptor.is_kept()
        else ""
    )
    scripts: list[str] = []
    if keep:
        config = json.dumps({"keepUrl": keep_url}).replace("<", "\\u003c")
        scripts.append(
            f"const config={config};const notice=document.getElementById('notice');\n"
            + _KEEP_HANDLER_JS
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
        f'<header><span class="spacer"></span>{keep}{notice}</header>\n' if keep or notice else ""
    )
    page_error = (
        '<div id="pageError" hidden role="status"><span id="pageErrorMessage"></span>'
        + (' <button id="askFix" type="button">Ask to fix</button>' if panel else "")
        + "</div>"
        if kind == "html"
        else ""
    )
    rows = "48px minmax(0,1fr)" if header else "minmax(0,1fr)"
    # Only the Keep and comment scripts call RCP; the error notice needs no connection.
    connects = bool(scripts)
    if kind == "html":
        scripts.insert(0, _error_shell_script(error_channel, inline=False))
    script_markup = "".join(f"<script>(()=>{{{script}}})();</script>" for script in scripts)
    document = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><script>(()=>{{{_THEME_SYNC_JS}}})();</script><style>
{_THEME_TOKENS_CSS}
*{{box-sizing:border-box}}html,body{{margin:0;height:100%;background:var(--paper);color:var(--ink);font:14px/1.45 var(--ui)}}
body{{display:grid;grid-template-rows:{rows}}}header{{display:flex;align-items:center;gap:12px;padding:0 16px;border-bottom:1px solid var(--rule);background:var(--panel)}}
.spacer{{flex:1}}
button{{border:1px solid var(--rule);background:transparent;color:var(--ink);padding:6px 10px;border-radius:var(--radius);font:inherit;cursor:pointer;box-shadow:var(--raised)}}button:disabled{{opacity:.45;cursor:default}}
main{{display:grid;grid-template-rows:minmax(0,1fr) auto;min-height:0}}
#pageError{{padding:6px 72px 6px 12px;min-width:0}}#pageError:not([hidden]){{display:flex;align-items:center;gap:8px}}
#pageErrorMessage{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}#askFix{{flex-shrink:0}}.canvas{{position:relative;min-width:0;min-height:0;background:white}}
iframe{{display:block;border:0;width:100%;height:100%}}.canvas>img{{display:block;width:100%;height:100%;object-fit:contain}}
{panel.style if panel else ""}</style></head><body>
{header}<main><div class="canvas">{preview}</div>{page_error}{panel.markup if panel else ""}</main>{script_markup}</body></html>"""
    csp = "default-src 'none'; script-src 'unsafe-inline'; "
    if connects:
        csp += "connect-src 'self'; "
    csp += "style-src 'unsafe-inline'; frame-src 'self'; img-src 'self' data: blob:; base-uri 'none'; form-action 'none'; object-src 'none'; frame-ancestors 'self'"
    return document, csp


def _inline_viewer_document(
    descriptor: AgentArtifactDescriptor,
    kind: str,
    *,
    content_url: str,
    live_url: str | None,
    selectable: bool,
) -> tuple[str, str]:
    """The same shell inside a reply: no chrome, transparent, sized by its content.

    The chat owns the artifact's actions around it. Agent HTML keeps its unchanged
    opaque sandbox one frame further in; this shell adds no capability to it.
    """

    error_channel = secrets.token_hex(16)
    if kind == "html":
        content_url = _error_content_url(content_url, error_channel)
    title = html.escape(descriptor.name, quote=True)
    url = html.escape(content_url, quote=True)
    preview = (
        f'<img id="previewImage" src="{url}" alt="{title}">'
        if kind == "image"
        else f'<iframe id="preview" sandbox="allow-scripts" title="{title}"></iframe>'
    )
    config = json.dumps(
        {
            "contentUrl": content_url,
            "maxHeight": ARTIFACT_INLINE_MAX_HEIGHT_PX,
            "initialHeight": ARTIFACT_INLINE_INITIAL_HEIGHT_PX,
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
    if live_url and kind == "html":
        scripts.append(
            "const liveUrl="
            + json.dumps(live_url).replace("<", "\\u003c")
            + ";\n"
            + f"const defaultLiveDelay={LIVE_ARTIFACT_REFRESH_SECONDS * 1000};\n"
            + resources.joinpath("artifact_live.js").read_text("utf-8")
        )
    script_markup = "".join(f"<script>(()=>{{{script}}})();</script>" for script in scripts)
    document = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><script>(()=>{{{_THEME_SYNC_JS}}})();</script><style>
{_THEME_TOKENS_CSS}
*{{box-sizing:border-box}}html,body{{margin:0;background:transparent;color:var(--ink);font:15px/1.5 var(--ui)}}
.canvas{{position:relative}}iframe{{display:block;border:0;width:100%;height:{ARTIFACT_INLINE_INITIAL_HEIGHT_PX}px;background:transparent}}
#previewImage{{display:block;max-width:100%;height:auto;border-radius:var(--radius)}}
.canvas:has(#previewImage){{display:inline-block;max-width:100%}}#boxLayer{{position:absolute;inset:0;cursor:crosshair}}
</style></head><body><main class="canvas">{preview}</main>{script_markup}</body></html>"""
    csp = (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "frame-src 'self'; img-src 'self' data: blob:; base-uri 'none'; form-action 'none'; "
        "object-src 'none'; frame-ancestors 'self'"
    )
    if live_url and kind == "html":
        csp += "; connect-src 'self'"
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
    # The page holds only RCP-rendered, escaped text; the one nonce-bound script is
    # RCP's own height report, so the document still runs no artifact code.
    nonce = secrets.token_urlsafe(18)
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
        "</style></head><body>"
        + marker
        + body
        + f'<script nonce="{nonce}">(()=>{{const root=document.documentElement;let last=-1;'
        "const report=()=>{const height=Math.ceil(root.getBoundingClientRect().height);"
        "if(height===last)return;last=height;"
        "parent.postMessage({type:'rcp-artifact-size',version:1,height},'*');};"
        "new ResizeObserver(report).observe(root);})();</script></body></html>",
        "sandbox allow-scripts; default-src 'none'; style-src 'unsafe-inline'; "
        f"script-src 'nonce-{nonce}'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'",
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

    `inline` renders the same bytes for a frame inside a reply: painted with the
    reply's theme and reporting its content height. Nothing else differs.
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
