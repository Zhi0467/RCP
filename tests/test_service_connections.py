from __future__ import annotations

import asyncio
import json
import stat
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from rcp import limits, transcription
from rcp.api import service_connections as routes
from rcp.api.dependencies import get_store
from rcp.api.identity import IdentityAccess
from rcp.service_connections import ConnectionError, ServiceConnections
from rcp.storage import AppStore
from rcp.transcription import ConnectRequest

KEY = "test-secret-never-real"
MIME = "audio/webm;codecs=opus"


def connection():
    return {
        **ConnectRequest(kind="openai_compatible", preset="openai", key=KEY).configuration(),
        "formats": [MIME],
        "verified_at": "test",
    }


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = AppStore(tmp_path / "rcp.sqlite3")
    member = store.local_owner.user_id
    private = ServiceConnections(store, member)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_store] = lambda: store
    monkeypatch.setattr(routes, "get_store", lambda _: store)
    identity = IdentityAccess(
        store, space_id=store.space_id, space_kind="personal", trusted_principal_resolver=None
    )
    monkeypatch.setattr(routes, "get_identity_access", lambda _: identity)
    with TestClient(app) as client:
        yield store, private, client


def test_keys_are_private_and_absent_from_responses_validation_and_logs(setup, monkeypatch, caplog):
    caplog.set_level("DEBUG")
    _, private, client = setup
    item = connection()

    async def check(_):
        return item

    monkeypatch.setattr(routes, "check_connection", check)
    response = client.post(
        "/api/service-connections",
        json={"kind": "openai_compatible", "preset": "openai", "key": KEY},
    )
    assert response.status_code == 200 and KEY not in response.text
    assert KEY not in client.get("/api/service-connections").text
    response = client.post("/api/service-connections", json={"kind": KEY, "key": KEY, "extra": KEY})
    assert response.status_code == 422 and KEY not in response.text
    oversized = KEY * limits.TRANSCRIPTION_KEY_MAX_CHARS
    response = client.post(
        "/api/service-connections",
        json={"kind": "openai_compatible", "preset": "openai", "key": oversized},
    )
    assert response.status_code == 422 and KEY not in response.text
    assert KEY not in caplog.text
    for path in [private.root.parent, private.root, *private.root.rglob("*")]:
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)


def mock_transport(monkeypatch, handler):
    original = httpx.AsyncClient
    options = []

    def client(**kwargs):
        options.append(kwargs)
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(transcription.httpx, "AsyncClient", client)
    return options


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["openai_compatible", "gemini"])
async def test_adapter_request_and_empty_success(monkeypatch, kind):
    request = ConnectRequest(
        kind=kind, preset="openai" if kind == "openai_compatible" else None, key=KEY
    )

    def handler(req):
        assert KEY not in str(req.url)
        if kind == "gemini":
            assert req.headers["x-goog-api-key"] == KEY
            body = json.loads(req.content)
            assert body["contents"][0]["parts"][0]["inlineData"] == {
                "mimeType": "audio/webm",
                "data": "YXVkaW8=",
            }
            assert req.url.path.endswith("/gemini-3.5-transcribe:generateContent")
        else:
            assert req.headers["authorization"] == f"Bearer {KEY}"
            assert req.url.path == "/v1/audio/transcriptions"
            assert b"audio" in req.content and b"gpt-transcribe" in req.content
        return httpx.Response(200, stream=httpx.ByteStream(b"{}"))

    options = mock_transport(monkeypatch, handler)
    assert await transcription.transcribe(request.configuration(), KEY, b"audio", MIME) == ""
    assert options == [{"trust_env": False, "follow_redirects": False}]


@pytest.mark.asyncio
async def test_gemini_reads_both_general_and_transcribe_model_replies(monkeypatch):
    request = ConnectRequest(kind="gemini", key=KEY)
    # The shape gemini-3.5-transcribe returned to a live probe, beside a general model's.
    parts = [{"audioTranscription": {"text": "Testing dictation"}}, {"text": " here."}]
    reply = {"candidates": [{"content": {"parts": parts, "role": "model"}}]}
    mock_transport(
        monkeypatch,
        lambda _: httpx.Response(200, stream=httpx.ByteStream(json.dumps(reply).encode())),
    )
    text = await transcription.transcribe(request.configuration(), KEY, b"audio", MIME)
    assert text == "Testing dictation here."


@pytest.mark.asyncio
async def test_upstream_errors_do_not_leak_keys_or_follow_redirects(monkeypatch, caplog):
    caplog.set_level("DEBUG")
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(
            302,
            headers={"location": "https://elsewhere.example"},
            stream=httpx.ByteStream(json.dumps({"error": {"message": KEY * 1000}}).encode()),
        )

    mock_transport(monkeypatch, handler)
    with pytest.raises(ConnectionError) as error:
        await transcription.transcribe(connection(), KEY, b"audio", MIME)
    assert error.value.code == "transcription_upstream_failed"
    assert KEY not in str(error.value) and KEY not in caplog.text
    assert len(calls) == 1
    assert len(transcription.sanitized("x" * 1000, KEY)) <= limits.TRANSCRIPTION_ERROR_MAX_CHARS


@pytest.mark.asyncio
async def test_a_reply_carrying_the_key_is_discarded_not_rewritten(monkeypatch):
    replies = iter([f"say {KEY} again", "say it again"])
    mock_transport(
        monkeypatch,
        lambda _: httpx.Response(
            200, stream=httpx.ByteStream(json.dumps({"text": next(replies)}).encode())
        ),
    )
    with pytest.raises(ConnectionError) as error:
        await transcription.transcribe(connection(), KEY, b"audio", MIME)
    assert error.value.code == "transcription_upstream_failed" and KEY not in str(error.value)
    assert await transcription.transcribe(connection(), KEY, b"audio", MIME) == "say it again"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "http://192.168.1.1",
        "https://user:pass@example.com",
        "https://example.com?x=1",
        "https://example.com#x",
        "file:///tmp/a",
        "https://example.com\\@evil",
    ],
)
def test_address_policy_refuses_before_transport(url):
    with pytest.raises(ConnectionError) as error:
        ConnectRequest(
            kind="openai_compatible", preset="custom", base_url=url, model="test"
        ).configuration()
    assert error.value.code == "address_not_allowed"


@pytest.mark.asyncio
@pytest.mark.parametrize("slow", [False, True])
async def test_outbound_response_is_bounded(monkeypatch, slow):
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(20):
                if slow:
                    await asyncio.sleep(0.02)
                yield b"xxxx"

    monkeypatch.setattr(limits, "TRANSCRIPTION_OUTBOUND_SECONDS", 0.03)
    monkeypatch.setattr(limits, "TRANSCRIPTION_RESPONSE_MAX_BYTES", 10)
    mock_transport(monkeypatch, lambda _: httpx.Response(200, stream=Stream()))
    with pytest.raises(ConnectionError) as error:
        await transcription.transcribe(connection(), KEY, b"audio", MIME)
    assert error.value.status == 502


@pytest.mark.asyncio
async def test_connect_records_only_passing_formats_and_never_saves_failure(
    setup, monkeypatch, tmp_path
):
    _, private, client = setup
    clips = tmp_path / "transcription_clips"
    clips.mkdir()
    for suffix in ("webm", "mp4"):
        (clips / f"check.{suffix}").write_bytes(b"mock clip")
    monkeypatch.setattr(transcription, "__file__", str(tmp_path / "transcription.py"))

    async def probe(_, key, audio, mime):
        if mime != MIME:
            raise ConnectionError("transcription_upstream_failed", 502)
        return "RCP dictation check."

    monkeypatch.setattr(transcription, "transcribe", probe)
    payload = {"kind": "openai_compatible", "preset": "openai", "key": KEY}
    response = client.post("/api/service-connections", json=payload)
    assert response.status_code == 200
    assert response.json()["formats"] == [MIME]

    async def fail(*_):
        raise ConnectionError("transcription_upstream_failed", 502)

    monkeypatch.setattr(transcription, "transcribe", fail)
    assert client.post("/api/service-connections", json=payload).status_code == 422

    async def silent(*_):
        return " "

    # A model that answers with no words cannot dictate, even when the request succeeds.
    monkeypatch.setattr(transcription, "transcribe", silent)
    assert client.post("/api/service-connections", json=payload).status_code == 422
    assert len(private.summary()["connections"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers,chunks",
    [
        ({}, [b"a"]),
        ({"content-length": "500"}, [b"a"]),
        ({"content-length": "1"}, [b"aa"]),
        ({"content-length": "1", "content-type": "text/plain"}, [b"a"]),
    ],
)
async def test_upload_bounds(headers, chunks, monkeypatch):
    monkeypatch.setattr(limits, "TRANSCRIPTION_AUDIO_MAX_BYTES", 10)
    headers = {"content-type": MIME, **headers}

    async def receive():
        return {"type": "http.request", "body": chunks.pop(0), "more_body": bool(chunks)}

    request = Request(
        {"type": "http", "headers": [(k.encode(), v.encode()) for k, v in headers.items()]}, receive
    )
    with pytest.raises(ConnectionError) as error:
        await routes.read_audio(request, [MIME])
    assert error.value.status == (415 if headers["content-type"] == "text/plain" else 413)


@pytest.mark.asyncio
async def test_upload_deadline_and_concurrency_slots_release(setup, monkeypatch):
    _, private, _ = setup
    monkeypatch.setattr(limits, "TRANSCRIPTION_UPLOAD_SECONDS", 0.01)
    monkeypatch.setattr(limits, "TRANSCRIPTION_CONCURRENT_PER_MEMBER", 1)

    async def receive():
        await asyncio.sleep(0.1)
        return {"type": "http.request", "body": b"a"}

    request = Request(
        {"type": "http", "headers": [(b"content-type", MIME.encode()), (b"content-length", b"1")]},
        receive,
    )
    with routes.transcription_slot(private):
        with pytest.raises(ConnectionError) as busy, routes.transcription_slot(private):
            pass
        assert busy.value.code == "transcription_busy"
        with pytest.raises(ConnectionError):
            await routes.read_audio(request, [MIME])
    with routes.transcription_slot(private):
        pass


def test_selection_disconnect_race_never_leaves_missing_selection(setup):
    _, private, client = setup
    item = connection()
    private.save(item, KEY)

    def select():
        try:
            private.select(item["id"])
        except ConnectionError as exc:
            assert exc.code == "connection_not_found"

    with ThreadPoolExecutor(2) as pool:
        results = [pool.submit(select), pool.submit(private.disconnect, item["id"])]
        for result in results:
            result.result()
    assert private.summary() == {"dictation": "system", "connections": []}
    response = client.post(
        f"/api/service-connections/{item['id']}/transcribe",
        content=b"a",
        headers={"content-type": MIME},
    )
    assert response.status_code == 404


def test_raw_upload_returns_text_without_persisting_audio(setup, monkeypatch):
    _, private, client = setup
    item = connection()
    private.save(item, KEY)

    async def transcribe(conn, key, audio, mime):
        assert conn["id"] == item["id"] and key == KEY and audio == b"audio" and mime == MIME
        return "transcript"

    monkeypatch.setattr(routes, "transcribe", transcribe)
    before = set(private.root.rglob("*"))
    response = client.post(
        f"/api/service-connections/{item['id']}/transcribe",
        content=b"audio",
        headers={"content-type": MIME},
    )
    assert response.json() == {"text": "transcript"}
    assert set(private.root.rglob("*")) == before
