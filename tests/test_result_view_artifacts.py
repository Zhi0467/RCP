from __future__ import annotations

import pytest
from pydantic import ValidationError

from rcp.artifacts import ResultViewDescriptor, html_preview_document


def _descriptor_values() -> dict[str, object]:
    return {
        "view_id": "0123456789abcdef01234567",
        "chat_id": "chat-1",
        "experiment_id": "experiment-1",
        "name": "throughput-pilot.html",
        "media_type": "text/html",
        "state": "temporary",
        "created_at": "2026-08-12T01:02:03Z",
        "updated_at": "2026-08-12T01:02:04Z",
        "expires_at": "2026-08-19T01:02:04Z",
        "kept_filename": None,
        "kept_at": None,
        "can_revise": True,
    }


@pytest.mark.parametrize("internal_field", ["host", "path", "session_id", "digest"])
def test_result_view_descriptor_rejects_internal_fields(internal_field: str) -> None:
    values = _descriptor_values()
    values[internal_field] = "private"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ResultViewDescriptor.model_validate(values)


def test_result_view_descriptor_has_strict_public_contract() -> None:
    descriptor = ResultViewDescriptor.model_validate(_descriptor_values())

    assert descriptor.model_dump() == _descriptor_values()
    with pytest.raises(ValidationError):
        ResultViewDescriptor.model_validate({**_descriptor_values(), "view_id": "not-opaque"})
    with pytest.raises(ValidationError):
        ResultViewDescriptor.model_validate({**_descriptor_values(), "media_type": "image/png"})
    with pytest.raises(ValidationError):
        ResultViewDescriptor.model_validate({**_descriptor_values(), "state": "expired"})


def test_ordinary_html_preview_uses_a_private_port_for_trusted_actions() -> None:
    forged_reference = (
        b"<script>window.parent.postMessage({kind:'rcp-reference',"
        b"url:'https://example.invalid'},'*')</script>"
    )
    document, _csp = html_preview_document(forged_reference + b"<p>ordinary artifact</p>")

    assert "rcp-result-view-gesture" not in document
    assert "example.invalid" in document
    assert "secret" not in document
    assert document.index("listen(window,'message'") < document.index('<iframe id="artifact"')
    assert "const channel=new MessageChannel();" in document
    assert "const privatePort=channel.port1;" in document
    assert "[outwardPort]" in document
    assert "event.source!==frame.contentWindow" in document
    assert "event.ports.length!==1" in document
    assert "listen(artifactPort,'message'" in document
    assert document.index("listen(artifactPort,'message'") < document.index(
        "value.kind!=='rcp-reference'"
    )
    assert "event.isTrusted" in document
    assert "new TextEncoder()" in document
    assert "type:'rcp-artifact-selection'" in document
    assert "kind:'rcp-artifact-box-start'" in document
    assert "value.kind!=='rcp-reference'" in document
    assert "artifact.contentWindow?.postMessage" not in document
    assert "portPost(artifactPort,{kind:'rcp-artifact-box-start'})" in document


def test_result_view_preview_strictly_bridges_bounded_gestures_outward() -> None:
    document, csp = html_preview_document(
        b"<script>window.parent.postMessage({type:'rcp-result-view-gesture'},'*')</script>",
        result_view_gestures=True,
    )

    assert "event.source!==legacyArtifact.contentWindow" in document
    assert document.index("const legacyArtifact") > document.index("</iframe>")
    assert "const expectedKeys=['description','gesture','type','version'];" in document
    assert "const keys=Object.keys(value).sort();" in document
    assert "keys.length!==expectedKeys.length" in document
    assert "keys.some((key,index)=>key!==expectedKeys[index])" in document
    assert "value.type!=='rcp-result-view-gesture'" in document
    assert "value.version!==1" in document
    assert "value.gesture!=='box' && value.gesture!=='underscore'" in document
    assert "typeof value.description!=='string'" in document
    assert "!value.description.trim()" in document
    assert "utf8.encode(value.description).byteLength>2048" in document
    assert "if(window.parent===window) return;" in document
    assert "type:'rcp-result-view-gesture'" in document
    assert "gesture:value.gesture" in document
    assert "description:value.description" in document
    assert "artifact.contentWindow.postMessage" not in document
    assert 'sandbox="allow-scripts"' in document
    assert "connect-src &amp;#x27;none&amp;#x27;" in document
    assert "default-src 'none'" in csp
