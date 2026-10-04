from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from rcp import limits, transcription
from rcp.api import voice as routes
from rcp.service_connections import VoiceSettings

from .test_service_connections import KEY, MIME, connection, mock_transport, setup  # noqa: F401

OFFER = {"sdp_offer": "v=0\r\n", "tools": []}
TOOLS = [
    {
        "type": "function",
        "name": "rcp_read",
        "description": "Read",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    }
]


@pytest.fixture
def voice_setup(setup):  # noqa: F811
    store, private, client = setup
    client.app.include_router(routes.router)
    return store, private, client


def reply(body, status=200):
    return httpx.Response(status, stream=httpx.ByteStream(json.dumps(body).encode()))


def enable(private):
    item = {**connection(), "purposes": ["voice"]}
    private.save(item, KEY)
    return item


def test_session_requires_explicit_voice_purpose(voice_setup):
    _, private, client = voice_setup
    legacy = connection()
    legacy.pop("purposes")
    private.save(legacy, KEY)
    response = client.post("/api/voice/sessions", json=OFFER)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "voice_not_connected"
    assert private.summary()["connections"][0]["purposes"] == ["transcription"]


def test_session_contract_and_no_session_persistence(voice_setup, monkeypatch):
    _, private, client = voice_setup
    enable(private)
    private.voice_settings(VoiceSettings(delegation_model="chosen-model"))
    before = {p: p.read_bytes() for p in private.root.rglob("*") if p.is_file()}

    def handler(request):
        assert request.method == "POST" and request.url.path == "/v1/live/sessions"
        assert request.headers["authorization"] == f"Bearer {KEY}"
        payload = json.loads(request.content)
        assert payload["transport"] == {"type": "webrtc", "sdp": OFFER["sdp_offer"]}
        assert payload["session"]["model"] == "gpt-live-1"
        delegation = payload["session"]["delegation"]
        assert delegation["type"] == "responses"
        assert delegation["responses"]["model"] == "chosen-model"
        assert delegation["responses"]["parallel_tool_calls"] is False
        assert delegation["responses"]["tools"] == TOOLS
        return reply({"transport": {"type": "webrtc", "sdp": "v=0\r\nanswer"}})

    mock_transport(monkeypatch, handler)
    response = client.post("/api/voice/sessions", json={**OFFER, "tools": TOOLS})
    assert response.status_code == 200
    assert response.json() == {
        "sdp_answer": "v=0\r\nanswer",
        "limits": {
            "idle_seconds": limits.VOICE_IDLE_SECONDS,
            "hard_cap_seconds": limits.VOICE_HARD_CAP_SECONDS,
            "confirm_timeout_seconds": limits.VOICE_CONFIRM_TIMEOUT_SECONDS,
            "commentary_max_chars": limits.VOICE_COMMENTARY_MAX_CHARS,
        },
    }
    assert before == {p: p.read_bytes() for p in private.root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("failure", ["error", "transport", "invalid", "secret"])
def test_upstream_failures_never_expose_secrets_or_raw_errors(voice_setup, monkeypatch, failure):
    _, private, client = voice_setup
    enable(private)
    calls = []
    raw = f"upstream private diagnostic {KEY} fragment {KEY[-8:]}"

    def handler(request):
        calls.append(request)
        if failure == "transport":
            raise httpx.ConnectError(raw)
        if failure == "secret":
            return reply({"transport": {"sdp": KEY}})
        if failure == "invalid":
            return reply({"transport": []})
        return reply({"error": {"message": raw}}, 401)

    mock_transport(monkeypatch, handler)
    response = client.post("/api/voice/sessions", json=OFFER)
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "voice_upstream_failed"
    assert KEY not in response.text and raw not in response.text and KEY[-8:] not in response.text
    assert len(response.text) < limits.TRANSCRIPTION_ERROR_MAX_CHARS
    assert len(calls) == 1


def test_voice_transport_deadline_is_an_upstream_failure(voice_setup, monkeypatch):
    _, private, client = voice_setup
    enable(private)

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(20):
                await asyncio.sleep(0.02)
                yield b"xxxx"

    monkeypatch.setattr(limits, "TRANSCRIPTION_OUTBOUND_SECONDS", 0.03)
    mock_transport(monkeypatch, lambda _: httpx.Response(200, stream=Stream()))
    assert client.post("/api/voice/sessions", json=OFFER).status_code == 502


def test_voice_only_connect_probes_model_once(voice_setup, monkeypatch):
    _, private, client = voice_setup
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET" and request.url.path == "/v1/models/gpt-live-1"
        assert request.headers["authorization"] == f"Bearer {KEY}"
        return reply({"id": "gpt-live-1"})

    mock_transport(monkeypatch, handler)
    response = client.post(
        "/api/service-connections",
        json={
            "kind": "openai_compatible",
            "preset": "openai",
            "key": KEY,
            "purposes": ["voice"],
        },
    )
    assert response.status_code == 200 and len(calls) == 1
    assert response.json()["formats"] == []
    assert private.voice_credentials()[0]["id"] == response.json()["id"]


def test_purpose_update_checks_only_additions_and_preserves_key(voice_setup, monkeypatch):
    _, private, client = voice_setup
    old = enable(private)
    item = connection()
    private.save(item, KEY)
    probes = []

    async def transcribe(*args):
        probes.append("transcription")
        return ""

    monkeypatch.setattr(transcription, "transcribe", transcribe)
    mock_transport(monkeypatch, lambda _: probes.append("voice") or reply({"id": "gpt-live-1"}))
    path = f"/api/service-connections/{item['id']}/purposes"
    for purposes in (["transcription", "voice"], ["transcription", "voice"], ["voice"]):
        assert client.put(path, json={"purposes": purposes}).status_code == 200
        assert private.credentials(item["id"])[0]["formats"] == item["formats"]
    assert probes == ["voice"]
    assert private.credentials(old["id"])[0]["purposes"] == []
    assert client.put(path, json={"purposes": ["voice", "transcription"]}).status_code == 200
    assert probes.count("transcription") == len(transcription.FORMATS)
    assert private.credentials(item["id"])[1] == KEY
    assert client.put(path, json={"purposes": []}).status_code == 200
    assert private.credentials(item["id"])[0]["purposes"] == []


def test_failed_purpose_probe_leaves_selection_and_payer_unchanged(voice_setup, monkeypatch):
    _, private, client = voice_setup
    old = enable(private)
    item = connection()
    private.save(item, KEY)
    private.select(item["id"])
    mock_transport(monkeypatch, lambda _: reply({"error": {"message": KEY}}, 403))
    response = client.put(
        f"/api/service-connections/{item['id']}/purposes", json={"purposes": ["voice"]}
    )
    assert response.status_code == 422 and KEY not in response.text
    assert private.voice_credentials()[0]["id"] == old["id"]
    assert private.summary()["dictation"] == item["id"]


def test_voice_requires_openai_preset_before_transport(voice_setup, monkeypatch):
    _, _, client = voice_setup
    calls = []
    mock_transport(monkeypatch, lambda req: calls.append(req))
    response = client.post(
        "/api/service-connections",
        json={
            "kind": "openai_compatible",
            "preset": "groq",
            "key": KEY,
            "purposes": ["voice"],
        },
    )
    assert response.status_code == 422 and not calls


def test_settings_default_roundtrip_and_preserve_dictation(voice_setup):
    _, private, client = voice_setup
    path = "/api/voice/settings"
    assert client.get(path).json() == {"delegation_model": "gpt-6-luna", "confirm": "tap"}
    value = {"delegation_model": "custom-model", "confirm": "none"}
    assert client.put(path, json=value).json() == value
    item = connection()
    private.save(item, KEY)
    private.select(item["id"])
    assert client.get(path).json() == value
    assert client.put(path, json={"confirm": "tap"}).json() == {**value, "confirm": "tap"}
    assert private.summary()["dictation"] == item["id"]


def test_voice_only_connection_cannot_be_used_for_dictation(voice_setup):
    _, private, client = voice_setup
    item = connection()
    private.save(item, KEY)
    private.select(item["id"])
    private.update_purposes(private.credentials(item["id"])[0], {**item, "purposes": ["voice"]})
    assert private.summary()["dictation"] == "system"
    assert (
        client.put("/api/service-connections/selection", json={"dictation": item["id"]}).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/service-connections/{item['id']}/transcribe",
            content=b"a",
            headers={"content-type": MIME},
        ).status_code
        == 409
    )


def test_session_validation_bounds_tools_and_does_not_echo_input(voice_setup, monkeypatch):
    _, _, client = voice_setup
    monkeypatch.setattr(limits, "VOICE_TOOLS_MAX_BYTES", 100)
    for body in (
        {**OFFER, "tools": TOOLS},
        {**OFFER, "tools": [{"type": KEY}]},
        {**OFFER, "extra": KEY},
    ):
        response = client.post("/api/voice/sessions", json=body)
        assert response.status_code == 422 and KEY not in response.text


def test_purpose_update_rechecks_connection_after_probe(voice_setup, monkeypatch):
    _, private, client = voice_setup
    item = connection()
    private.save(item, KEY)

    def handler(_):
        private.disconnect(item["id"])
        return reply({"id": "gpt-live-1"})

    mock_transport(monkeypatch, handler)
    response = client.put(
        f"/api/service-connections/{item['id']}/purposes", json={"purposes": ["voice"]}
    )
    assert response.status_code == 404 and private.summary()["connections"] == []
