"""Self-contained browser rendering from the source, wheel or frozen Web bundle."""

from __future__ import annotations

import json
import re

from rcp.artifacts import FrameAddon
from rcp.limits import ARTIFACT_ERROR_MAX_CHARS
from rcp.web_assets import web_dist_path


class ArtifactRendererUnavailable(ValueError):
    pass


def renderer_assets() -> tuple[str, str]:
    root = web_dist_path()
    try:
        script = (root / "artifact-renderer.js").read_text(encoding="utf-8")
        style = (root / "artifact-renderer.css").read_text(encoding="utf-8")
    except OSError as exc:
        raise ArtifactRendererUnavailable(
            "The artifact math renderer is unavailable. Rebuild the Web assets."
        ) from exc
    if not script.strip() or not style.strip():
        raise ArtifactRendererUnavailable(
            "The artifact math renderer is empty. Rebuild the Web assets."
        )
    return re.sub(
        "</script", lambda match: "<\\/" + match.group(0)[2:], script, flags=re.IGNORECASE
    ), style


def paper_math_addon(warnings: list[str]) -> FrameAddon:
    script, style = renderer_assets()
    warning = "; ".join(warnings)[:ARTIFACT_ERROR_MAX_CHARS]
    notice = json.dumps(warning).replace("<", "\\u003c")
    return FrameAddon(
        frame_script=(
            script + "\nlisten(document,'DOMContentLoaded',()=>{"
            "RcpArtifactRenderer.renderHtmlMath(document.body);"
            f"const warning={notice};if(warning) recordError(warning);"
            "});\n"
        ),
        wrapper_script="",
        frame_style=style,
    )
