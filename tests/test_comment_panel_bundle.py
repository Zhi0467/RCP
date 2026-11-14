import re
from pathlib import Path

import pytest

from rcp import artifact_comments
from rcp.artifact_views import artifact_viewer_document
from rcp.artifacts import AgentArtifactDescriptor


def test_viewer_inlines_shared_comments_bundle():
    panel = artifact_comments.comment_panel({"projectId": "p", "artifactId": "a"})
    document, csp = artifact_viewer_document(
        AgentArtifactDescriptor(
            artifact_id="a" * 24, name="figure.html", media_type="text/html", size_bytes=10
        ),
        content_url="/preview",
        state="temporary",
        panel=panel,
    )
    root = artifact_comments.web_dist_path()
    assert (root / "comments.css").read_text() in document
    script = (root / "comments.js").read_text()
    escaped = re.sub("</script", lambda match: "<\\/" + match.group(0)[2:], script, flags=re.I)
    assert escaped in document
    assert "RcpComments" in document
    assert "mountViewer" in document
    assert "script-src 'unsafe-inline'" in csp
    assert 'id="comments-root"' in document


@pytest.mark.parametrize("assets", [None, ""])
def test_comment_panel_requires_built_assets(tmp_path: Path, monkeypatch, assets):
    monkeypatch.setattr(artifact_comments, "web_dist_path", lambda: tmp_path, raising=False)
    if assets is not None:
        (tmp_path / "comments.js").write_text(assets)
        (tmp_path / "comments.css").write_text(assets)
    with pytest.raises(ValueError, match="Rebuild the Web assets"):
        artifact_comments.comment_panel({"projectId": "p", "artifactId": "a"})
