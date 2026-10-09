"""Artifact instructions rendered from the same types and limits as enforcement."""

from __future__ import annotations

import json
from typing import Literal, get_args, get_origin

from pydantic import BaseModel

from rcp.artifact_preview import PREVIEW_COMMAND
from rcp.artifact_theme import ARTIFACT_THEME_TOKENS
from rcp.artifacts import artifact_view
from rcp.limits import (
    ARTIFACT_INLINE_MAX_HEIGHT_PX,
    ARTIFACT_PREVIEW_IDLE_SECONDS,
    CHAT_ARTIFACT_MAX_COUNT,
    CHAT_ARTIFACT_MAX_FILE_BYTES,
    CHAT_ARTIFACT_MAX_TOTAL_BYTES,
    LIVE_ARTIFACT_LOG_TAIL_LINES,
    LIVE_ARTIFACT_MAX_BYTES,
    LIVE_ARTIFACT_MAX_FILES,
    LIVE_ARTIFACT_MAX_NEEDS,
    LIVE_ARTIFACT_MAX_ROWS,
    LIVE_ARTIFACT_MAX_SCANNED_ENTRIES,
    LIVE_ARTIFACT_MAX_TOTAL_BYTES,
    LIVE_ARTIFACT_REFRESH_SECONDS,
    LIVE_ARTIFACT_SSH_REFRESH_SECONDS,
)
from rcp.live_artifacts import (
    NEED_SNAPSHOT_MODELS,
    EpisodeNeed,
    FileSnapshot,
    FilesSnapshot,
    JobSnapshot,
    LiveDataMessage,
    LiveEvidence,
    LiveSnapshot,
    LiveTag,
)
from rcp.storage.artifact_models import Artifact


def _allowed(annotation: object) -> str:
    return " or ".join(json.dumps(value) for value in get_args(annotation))


def _fields(model: type[BaseModel], *, indent: str, skip: tuple[str, ...] = ()) -> list[str]:
    lines = []
    for name, field in model.model_fields.items():
        if name in skip:
            continue
        allowed = (
            f" One of {_allowed(field.annotation)}."
            if get_origin(field.annotation) is Literal
            else ""
        )
        lines.append(f"{indent}- `{name}`: {field.description}{allowed}")
    return lines


def _need_shape(need: type[BaseModel]) -> str:
    shape = {
        name: (field.default if name == "kind" else f"<{name}>")
        for name, field in need.model_fields.items()
    }
    return json.dumps(shape)


def live_contract(*, allow_episode: bool = False) -> str:
    example = LiveDataMessage(
        snapshots=[JobSnapshot(key="training", state="running")],
        refresh_seconds=LIVE_ARTIFACT_REFRESH_SECONDS,
    ).model_dump_json()
    lines = [
        "Live HTML pages (optional):",
        "- Use one only for data still changing after your turn, such as a training job, a sweep, or a node gathering Evidence. A finished result is an ordinary page.",
        '- Declare the sources once: <script type="application/json" id="rcp-live">'
        + json.dumps({"version": 1, "needs": [{"kind": "job", "key": "training"}]})
        + "</script>. RCP binds them once per artifact version.",
        *_fields(LiveTag, indent="  "),
        "- RCP's viewer posts the data into the page. Listen for window `message` events whose `event.data.kind` is `rcp-live-data`; never fetch RCP endpoints. Example message: "
        + example,
        *_fields(LiveDataMessage, indent="  "),
        "- Source kinds. Each snapshot also carries `error`: "
        + (LiveSnapshot.model_fields["error"].description or ""),
    ]
    for need, snapshot in NEED_SNAPSHOT_MODELS:
        if need is EpisodeNeed and not allow_episode:
            continue
        lines.append(f"  - {_need_shape(need)}: {need.model_fields['kind'].description}")
        lines.extend(_fields(need, indent="    ", skip=("kind",)))
        lines.append("    Snapshot fields:")
        lines.extend(_fields(snapshot, indent="    ", skip=("kind", "error")))
        if snapshot is FilesSnapshot:
            lines.append("    Each matched file has:")
            lines.extend(_fields(FileSnapshot, indent="      ", skip=("kind",)))
    lines.extend(
        [
            "  Each Evidence entry has:",
            *_fields(LiveEvidence, indent="  "),
            f"- Limits: at most {LIVE_ARTIFACT_MAX_NEEDS} sources; file reads cap at {LIVE_ARTIFACT_MAX_ROWS} rows and {LIVE_ARTIFACT_MAX_BYTES} bytes; folder sources cap at {LIVE_ARTIFACT_MAX_FILES} files and {LIVE_ARTIFACT_MAX_TOTAL_BYTES} total bytes and list at most {LIVE_ARTIFACT_MAX_SCANNED_ENTRIES} directory entries, so keep the folder narrow; job logs keep the last {LIVE_ARTIFACT_LOG_TAIL_LINES} lines.",
            f"- The open viewer refreshes every {LIVE_ARTIFACT_REFRESH_SECONDS} seconds, or {LIVE_ARTIFACT_SSH_REFRESH_SECONDS} seconds for SSH files. When every watched job or episode ends, RCP saves a complete final snapshot. A page watching only nodes, files, and folders never becomes final.",
            "- An invalid declaration leaves the page static with a notice. Show incomplete data and source errors as unavailable, never as zero. A report is never live.",
            "- Sources are limited to registered project repository paths (the repositories listed in this prompt), this artifact's own ready conversation/episode worktree, and this artifact's graph target. RCP run stages (conversation workspace, turn folders, artifact folders) are not readable; a path alone grants no access. Scripts can still send received data out by navigating their own frame, so do not assume zero network access.",
            "- The live-pages skill has worked examples.",
        ]
    )
    return "\n".join(lines)


def artifact_contract(artifact_path: str, *, allow_episode: bool = False) -> str:
    media_types = get_args(Artifact.model_fields["media_type"].annotation)
    viewable = ", ".join(
        media_type for media_type in media_types if artifact_view(media_type) not in {"file", "pdf"}
    )
    return f"""Reply and artifact contract:
- The final assistant message is the complete independent Markdown reply the human reads.
- Cite a file with an ordinary Markdown link to its absolute path on its host; add a `:line` suffix only for a repository file. Only an authorized repository file or a file in the turn's artifact directory resolves in RCP.
- Artifacts are optional; an empty directory is normal. RCP discovers direct regular files in `{artifact_path}`, at most {CHAT_ARTIFACT_MAX_COUNT} files, {CHAT_ARTIFACT_MAX_FILE_BYTES} bytes per file and {CHAT_ARTIFACT_MAX_TOTAL_BYTES} bytes total. Do not use nested directories or symlinks.
- Viewable media types: {viewable}. PDFs open separately; other types are download-only. Artifact cards offer Download and Keep.
- When a turn names a commented artifact's writable path, edit that file in place. Its turn names the destination; do not choose a new output path for that edit.
- HTML must be self-contained. Ordinary HTTP(S) reference links are allowed; external scripts, images, fonts, fetches, and other resource loads are blocked in the preview.

{inline_contract(artifact_path)}

{live_contract(allow_episode=allow_episode)}"""


def inline_contract(artifact_path: str) -> str:
    """How a reply shows an artifact in place, from the tokens and limits RCP applies."""

    tokens = ", ".join(
        f"`--rcp-{name}` ({purpose})" for name, purpose in ARTIFACT_THEME_TOKENS.items()
    )
    return "\n".join(
        [
            "Artifacts inside the reply:",
            f"- To show a viewable artifact where it belongs in the reply, embed it with Markdown image syntax and its absolute path: `![Short title]({artifact_path}/name.html)`. RCP renders it there, running and interactive: HTML runs its scripts, images and SVG draw, Markdown and text render. The reader can play with it in place, expand it, keep it, and comment on it to ask for an edit. Write the file first; an embedded file that does not exist shows nothing. An artifact you do not embed is shown as a card below the reply, and an ordinary link opens it in the viewer instead.",
            "- Use an embedded HTML page whenever interaction helps the reader: a figure to hover or filter, a parameter to drag, a diagram to explore, a simulation or a game. Use SVG or an image for a figure that only needs to be looked at. Keep prose in the reply itself, not in the page.",
            "- Embed only what the reader will use. A reply that prose answers needs no artifact; one page per idea is enough, and a reply rarely needs more than two. Draw from the project's real data when it exists, and label anything simulated or illustrative as such.",
            f"- The page's own content sets its height, up to {ARTIFACT_INLINE_MAX_HEIGHT_PX}px, after which it scrolls inside itself. Size from content and width: never from the viewport (no `100vh`, no full-window layouts). Scale a canvas to the frame's width with a fixed aspect ratio.",
            f"- RCP paints an embedded page with the reply's theme. These CSS custom properties are set on `:root`: {tokens}. RCP fixes the page's `color-scheme` to the reply's light or dark mode, and `--rcp-color-mode` names it, so do not set `color-scheme` yourself. Only the reply sets them; Expand, a download, and a browser check do not, so give each one a fallback, as in `var(--rcp-ink, #1f2328)`. Leave the page background transparent and draw cards with `--rcp-panel` and `--rcp-rule` so the page reads as part of the reply. A page drawn for one fixed palette, such as an arcade game, paints its own background and text colors.",
            "- Inside a reply, handle keys on your own focusable element rather than `window`, start with sound off, set `touch-action: none` on a surface the pointer drags, and support pointer, touch, and keyboard input.",
            f"- To check a page when this turn has a browser, run `{PREVIEW_COMMAND} <dir> --port 0`. It prints a loopback URL and serves the page as RCP's viewer does: the same opaque sandbox and the same removal of external `src`, `href`, and other loading attributes, without the reply's palette, so the page shows its fallback colors. The console names each loading attribute RCP removed; check the page also drew what you expect. The preview exits on its own after {ARTIFACT_PREVIEW_IDLE_SECONDS} seconds without a request. Open the printed URL with `goto`, read the console for errors, and stop the server when you are done if your shell allows it; the browser refuses `file:` URLs. Without a browser grant, do not install or launch one: check the script's logic directly, and say in the reply what you could not render.",
        ]
    )
