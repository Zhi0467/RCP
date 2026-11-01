from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from rcp import limits, transcription, voice
from rcp.service_connections import VoiceSettings

from .test_service_connections import KEY, MIME, connection, mock_transport, setup  # noqa: F401

THREAD_ID = "d2ae284d-886f-4b98-9840-8907a66ee0cb"
OFFER = {"sdp_offer": "v=0\r\n", "tools": [], "playbook": "How RCP works.", "thread_id": THREAD_ID}
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
    with store.connection() as db:
        db.execute(
            "INSERT INTO agent_threads (thread_id,user_id,kind,created_at) VALUES (?,?,?,?)",
            (THREAD_ID, private.member_id, "space", store.now()),
        )
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


def test_session_contract_does_not_allocate_a_record(voice_setup, monkeypatch):
    _, private, client = voice_setup
    enable(private)
    private.voice_settings(
        VoiceSettings(delegation_model="chosen-model", live_model="gpt-live-2", idle_minutes=7)
    )
    before = {p: p.read_bytes() for p in private.root.rglob("*") if p.is_file()}

    def handler(request):
        assert request.method == "POST" and request.url.path == "/v1/live/sessions"
        assert request.headers["authorization"] == f"Bearer {KEY}"
        payload = json.loads(request.content)
        assert payload["transport"] == {"type": "webrtc", "sdp": OFFER["sdp_offer"]}
        assert payload["session"]["model"] == "gpt-live-2"
        delegation = payload["session"]["delegation"]
        assert delegation["type"] == "responses"
        assert delegation["responses"]["model"] == "chosen-model"
        assert delegation["responses"]["parallel_tool_calls"] is False
        assert delegation["responses"]["tools"] == [{**tool, "strict": False} for tool in TOOLS]
        # Both models get the fixed instructions, then the page's playbook.
        for instructions in (
            payload["session"]["instructions"],
            delegation["responses"]["instructions"],
        ):
            assert instructions.startswith(voice.INSTRUCTIONS)
            assert instructions.endswith(OFFER["playbook"])
        return reply({"transport": {"type": "webrtc", "sdp": "v=0\r\nanswer"}})

    mock_transport(monkeypatch, handler)
    response = client.post("/api/voice/sessions", json={**OFFER, "tools": TOOLS})
    assert response.status_code == 200
    assert {key: response.json()[key] for key in ("sdp_answer", "limits")} == {
        "sdp_answer": "v=0\r\nanswer",
        "limits": {
            "idle_seconds": 7 * 60,
            "hard_cap_seconds": limits.VOICE_HARD_CAP_SECONDS,
            "confirm_timeout_seconds": limits.VOICE_CONFIRM_TIMEOUT_SECONDS,
            "commentary_max_chars": limits.VOICE_COMMENTARY_MAX_CHARS,
            "transcript_entry_max_bytes": limits.VOICE_TRANSCRIPT_ENTRY_MAX_BYTES,
            "transcript_session_max_bytes": limits.VOICE_TRANSCRIPT_SESSION_MAX_BYTES,
            "transcript_max_entries": limits.VOICE_TRANSCRIPT_MAX_ENTRIES,
            "transcript_max_receipts": limits.VOICE_TRANSCRIPT_MAX_RECEIPTS,
        },
    }
    assert all(path.read_bytes() == data for path, data in before.items())
    assert set(response.json()) == {"sdp_answer", "input_truncated", "limits"}
    assert not list(private.root.rglob("voice-sessions/*.json"))


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


def test_voice_only_connect_probes_live_and_thinking_models_once(voice_setup, monkeypatch):
    _, private, client = voice_setup
    calls = []

    def handler(request):
        calls.append(request.url.path)
        assert request.method == "GET"
        assert request.headers["authorization"] == f"Bearer {KEY}"
        return reply({"id": request.url.path.rsplit("/", 1)[1]})

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
    assert response.status_code == 200
    assert calls == ["/v1/models/gpt-live-1", "/v1/models/gpt-6-luna"]
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
        return "RCP dictation check."

    monkeypatch.setattr(transcription, "transcribe", transcribe)

    def handler(request):
        probes.append("voice")
        return reply({"id": request.url.path.rsplit("/", 1)[1]})

    mock_transport(monkeypatch, handler)
    path = f"/api/service-connections/{item['id']}"
    for purposes in (["transcription", "voice"], ["transcription", "voice"], ["voice"]):
        assert client.put(path, json={"purposes": purposes}).status_code == 200
        assert private.credentials(item["id"])[0]["formats"] == item["formats"]
    # Adding voice checks the live model and the thinking model the new payer must reach.
    assert probes == ["voice", "voice"]
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
    response = client.put(f"/api/service-connections/{item['id']}", json={"purposes": ["voice"]})
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
    models = {
        "delegation_model": "gpt-6-luna",
        "live_model": "gpt-live-1",
        "idle_minutes": limits.VOICE_IDLE_MINUTES_DEFAULT,
    }
    assert client.get(path).json() == {**models, "confirm": "tap"}
    assert client.put(path, json={"confirm": "none"}).json() == {**models, "confirm": "none"}
    # Each field saves alone and keeps the other; the idle limit stays in its range.
    saved = client.put(path, json={"idle_minutes": limits.VOICE_IDLE_MINUTES_MAX}).json()
    assert saved == {**models, "confirm": "none", "idle_minutes": limits.VOICE_IDLE_MINUTES_MAX}
    for bad in ({}, {"idle_minutes": limits.VOICE_IDLE_MINUTES_MAX + 1}, {"idle_minutes": 0}):
        assert client.put(path, json=bad).status_code == 422
    models["idle_minutes"] = limits.VOICE_IDLE_MINUTES_MAX
    # Voice models change only through the checked connection update.
    unchecked = client.put(path, json={"confirm": "tap", "delegation_model": "unchecked"})
    assert unchecked.status_code == 422
    item = connection()
    private.save(item, KEY)
    private.select(item["id"])
    assert client.get(path).json() == {**models, "confirm": "none"}
    assert private.summary()["dictation"] == item["id"]


def test_voice_only_connection_cannot_be_used_for_dictation(voice_setup):
    _, private, client = voice_setup
    item = connection()
    private.save(item, KEY)
    private.select(item["id"])
    private.update_connection(private.credentials(item["id"])[0], {**item, "purposes": ["voice"]})
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
        {**OFFER, "playbook": "x" * (limits.VOICE_PLAYBOOK_MAX_CHARS + 1)},
    ):
        response = client.post("/api/voice/sessions", json=body)
        assert response.status_code == 422 and KEY not in response.text


def test_purpose_update_rechecks_connection_after_probe(voice_setup, monkeypatch):
    _, private, client = voice_setup
    item = connection()
    private.save(item, KEY)

    def handler(request):
        if private.summary()["connections"]:
            private.disconnect(item["id"])
        return reply({"id": request.url.path.rsplit("/", 1)[1]})

    mock_transport(monkeypatch, handler)
    response = client.put(f"/api/service-connections/{item['id']}", json={"purposes": ["voice"]})
    assert response.status_code == 404 and private.summary()["connections"] == []


def test_model_lists_filter_by_purpose_and_hide_retiring_ids(voice_setup, monkeypatch):
    _, private, client = voice_setup
    openai_ids = [
        {"id": "gpt-transcribe"},
        {"id": "whisper-1"},
        {"id": "gpt-4o-transcribe-diarize"},
        {"id": "gpt-6-luna"},
        {"id": "gpt-6-sol"},
        {"id": "gpt-live-1"},
        {"id": "gpt-old-transcribe", "shutdown_date": "2026-11-01"},
        {"id": "gpt-live-transcribe"},
        {"id": "gpt-6-sol-2026-08-01"},
        {"id": "gpt-3.5-turbo-instruct"},
        {"id": "text-embedding-3"},
        {"id": f"gpt-6-{KEY}"},
    ]
    gemini_models = [
        {"name": "models/gemini-3.5-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.5-transcribe", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/embedding-2", "supportedGenerationMethods": ["embedContent"]},
    ]

    def handler(request):
        if request.url.host == "generativelanguage.googleapis.com":
            assert request.headers["x-goog-api-key"] == KEY
            return reply({"models": gemini_models})
        assert request.url.path == "/v1/models"
        return reply({"data": openai_ids})

    mock_transport(monkeypatch, handler)
    openai = {"kind": "openai_compatible", "preset": "openai", "key": KEY}
    response = client.post("/api/service-connections/models", json=openai)
    # Streaming-only, dated, and pre-delegation ids drop out; newest models come first.
    assert response.json() == {
        "transcription": ["gpt-transcribe", "whisper-1"],
        "delegation": ["gpt-6-sol", "gpt-6-luna"],
        "live": ["gpt-live-1"],
    }
    assert KEY not in response.text
    item = connection()
    private.save(item, KEY)
    stored = client.get(f"/api/service-connections/{item['id']}/models")
    assert stored.json() == response.json()
    gemini = client.post("/api/service-connections/models", json={"kind": "gemini", "key": KEY})
    assert gemini.json() == {
        "transcription": ["gemini-3.5-transcribe"],
        "delegation": [],
        "live": [],
    }


def test_model_list_failure_is_reported_without_the_key(voice_setup, monkeypatch):
    _, _, client = voice_setup
    mock_transport(monkeypatch, lambda _: reply({"error": {"message": KEY}}, 401))
    response = client.post(
        "/api/service-connections/models",
        json={"kind": "openai_compatible", "preset": "openai", "key": KEY},
    )
    assert response.status_code == 502 and KEY not in response.text
    assert response.json()["detail"]["code"] == "service_access_denied"
    mock_transport(monkeypatch, lambda _: reply({}, 500))
    response = client.post(
        "/api/service-connections/models",
        json={"kind": "openai_compatible", "preset": "openai", "key": KEY},
    )
    assert response.json()["detail"]["code"] == "model_list_failed"


def test_edit_rechecks_only_changed_models_and_saves_delegation(voice_setup, monkeypatch):
    _, private, client = voice_setup
    # A dictation-only OpenAI connection still carries the account's thinking model.
    item = connection()
    private.save(item, KEY)
    probes = []

    async def transcribe(connection, *args):
        probes.append(connection["model"])
        return "RCP dictation check."

    def handler(request):
        probes.append(request.url.path)
        model = request.url.path.rsplit("/", 1)[1]
        return reply({}, 404) if model == "gpt-missing" else reply({"id": model})

    monkeypatch.setattr(transcription, "transcribe", transcribe)
    mock_transport(monkeypatch, handler)
    path = f"/api/service-connections/{item['id']}"
    purposes = item["purposes"]
    unchanged = {"purposes": purposes, "model": item["model"], "delegation_model": "gpt-6-luna"}
    assert client.put(path, json=unchanged).status_code == 200 and probes == []
    changed = {"purposes": purposes, "model": "whisper-1", "delegation_model": "gpt-6-sol"}
    assert client.put(path, json=changed).status_code == 200
    assert sorted(probes) == ["/v1/models/gpt-6-sol"] + ["whisper-1"] * len(transcription.FORMATS)
    assert private.credentials(item["id"])[0]["model"] == "whisper-1"
    assert private.voice_settings()["delegation_model"] == "gpt-6-sol"
    refused = {**changed, "delegation_model": "gpt-missing"}
    response = client.put(path, json=refused)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "connection_check_failed"
    assert private.voice_settings()["delegation_model"] == "gpt-6-sol"


def test_thinking_model_is_checked_with_the_voice_key_and_refuses_a_concurrent_change(
    voice_setup, monkeypatch
):
    _, private, client = voice_setup
    payer = enable(private)
    other_key = "other-test-secret-never-real"
    other = connection()
    private.save(other, other_key)
    seen = []
    race = []

    def handler(request):
        seen.append(request.headers["authorization"])
        if race:
            race.pop()()
        return reply({"id": request.url.path.rsplit("/", 1)[1]})

    mock_transport(monkeypatch, handler)
    path = f"/api/service-connections/{other['id']}"
    body = {"purposes": ["transcription"], "delegation_model": "gpt-6-sol"}
    assert client.put(path, json=body).status_code == 200
    assert seen == [f"Bearer {KEY}"]
    assert private.credentials(payer["id"])[0]["purposes"] == ["voice"]

    # Another request changes the model, then moves voice, while each check runs.
    race.append(lambda: private.voice_settings(VoiceSettings(delegation_model="gpt-6-luna")))
    response = client.put(path, json={**body, "delegation_model": "gpt-6-nova"})
    assert response.status_code == 409, response.text
    assert private.voice_settings()["delegation_model"] == "gpt-6-luna"
    third = connection()
    private.save(third, "third-test-secret-never-real")
    race.append(lambda: private.update_connection(third, {**third, "purposes": ["voice"]}))
    response = client.put(path, json={**body, "delegation_model": "gpt-6-nova"})
    assert response.status_code == 409, response.text
    assert private.voice_settings()["delegation_model"] == "gpt-6-luna"


def test_model_only_save_keeps_uses_changed_elsewhere(voice_setup, monkeypatch):
    _, private, client = voice_setup
    stale = enable(private)
    mock_transport(monkeypatch, lambda request: reply({"id": request.url.path.rsplit("/", 1)[1]}))
    # Another tab moves voice to a second connection while this card is open.
    moved = connection()
    private.save(moved, "moved-test-secret-never-real")
    current = private.update_connection(moved, {**moved, "purposes": ["voice"]})
    assert private.credentials(stale["id"])[0]["purposes"] == []
    path = f"/api/service-connections/{stale['id']}"
    response = client.put(path, json={"delegation_model": "gpt-6-sol"})
    assert response.status_code == 200, response.text
    assert private.credentials(stale["id"])[0]["purposes"] == []
    assert private.voice_credentials()[0]["id"] == current["id"]


def test_edited_key_in_a_voice_model_is_refused_before_the_payer_checks_it(
    voice_setup, monkeypatch
):
    _, private, client = voice_setup
    enable(private)
    other_key = "other-test-secret-never-real"
    other = connection()
    private.save(other, other_key)
    seen = []
    mock_transport(monkeypatch, lambda request: seen.append(request.url) or reply({}))
    path = f"/api/service-connections/{other['id']}"
    response = client.put(path, json={"delegation_model": f"gpt-{other_key}"})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "connection_check_failed"
    assert other_key not in response.text and seen == []


def test_removed_record_routes_and_resume_are_refused(voice_setup):
    _, _, client = voice_setup
    # The built web app's GET fallback turns some of these 404s into 405s.
    for method, path in [
        ("get", "/api/voice/sessions"),
        ("put", "/api/voice/sessions/old"),
        ("delete", "/api/voice/sessions/old"),
        ("get", "/api/voice/sessions/old/generation"),
    ]:
        assert getattr(client, method)(path).status_code in {404, 405}
    assert client.post("/api/voice/sessions", json={**OFFER, "resume_id": "old"}).status_code == 422


def test_voice_entries_are_private_idempotent_and_seed_sessions(voice_setup, monkeypatch):
    store, private, client = voice_setup
    enable(private)
    entries = [
        {
            "entry_id": str(i),
            "speaker": "member" if i % 2 else "agent",
            "text": str(i),
            "at": store.now(),
        }
        for i in range(150)
    ]
    path = f"/api/agent-threads/{THREAD_ID}/voice-entries"

    def rail_version():
        body = {"client_id": "c", "open": [], "due": [], "agent_threads": True}
        return client.post("/api/resource-watermarks", json=body).json()["agent_threads"]

    before = rail_version()
    response = client.post(path, json={"entries": entries})
    assert response.status_code == 200, response.text
    assert response.json() == {"appended": 150}
    moved = rail_version()
    assert moved != before
    assert client.post(path, json={"entries": entries}).json() == {"appended": 0}
    assert rail_version() == moved
    transcript = store.path.parent / "agent-threads" / private.member_id / f"{THREAD_ID}.jsonl"
    records = [json.loads(line) for line in transcript.read_text().splitlines()]
    assert len(records) == 150
    assert all(
        row["source"] == "voice"
        and row["privateOwner"] == private.member_id
        and row.get("operationId") is None
        for row in records
    )
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return reply({"transport": {"sdp": "answer"}})

    mock_transport(monkeypatch, handler)
    response = client.post("/api/voice/sessions", json=OFFER)
    assert response.status_code == 200, response.text
    assert response.json()["input_truncated"]
    inputs = payloads[-1]["session"]["input"]
    assert 0 < len(inputs) <= limits.VOICE_RESUME_MAX_MESSAGES
    assert inputs[-1]["content"][0]["text"].endswith("149")
    assert (
        sum(
            len(row["content"][0]["text"].encode()) + limits.VOICE_RESUME_MESSAGE_OVERHEAD_TOKENS
            for row in inputs
        )
        <= limits.VOICE_RESUME_MAX_TOKENS
    )
    with store.connection() as db:
        db.execute(
            "UPDATE agent_threads SET user_id='someone-else' WHERE thread_id=?", (THREAD_ID,)
        )
    assert client.post(path, json={"entries": entries}).status_code == 404
    assert client.get(f"/api/agent-threads/{THREAD_ID}/memory").status_code == 404
    assert client.post("/api/voice/sessions", json=OFFER).status_code == 404


def test_old_voice_records_expire_without_becoming_thread_history(voice_setup, monkeypatch):
    _, private, client = voice_setup
    record = private.claim_voice_session()
    enable(private)
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return reply({"transport": {"sdp": "answer"}})

    mock_transport(monkeypatch, handler)
    assert client.post("/api/voice/sessions", json=OFFER).status_code == 200
    assert payloads[-1]["session"]["input"] == []
    record["updated_at"] -= limits.VOICE_TRANSCRIPT_RETENTION_SECONDS + 1
    private._voice_path(record["id"]).write_text(json.dumps(record))
    assert client.post("/api/voice/sessions", json=OFFER).status_code == 200
    assert not private._voice_path(record["id"]).exists()


def test_voice_entry_bounds_and_space_memory(voice_setup, monkeypatch):
    store, _, client = voice_setup
    path = f"/api/agent-threads/{THREAD_ID}/voice-entries"
    entry = {"entry_id": "entry", "speaker": "member", "text": "hello", "at": store.now()}
    assert (
        client.post(
            path, content=iter([b"{}"]), headers={"content-type": "application/json"}
        ).status_code
        == 411
    )
    oversized = {**entry, "text": "漢" * limits.VOICE_TRANSCRIPT_ENTRY_MAX_BYTES}
    assert client.post(path, json={"entries": [oversized]}).status_code == 422
    assert client.get(f"/api/agent-threads/{THREAD_ID}/memory").json() == {"truncated": False}
    monkeypatch.setattr(limits, "VOICE_TRANSCRIPT_REQUEST_MAX_BYTES", 50)
    assert client.post(path, json={"entries": [entry]}).status_code == 413


def test_thread_transcript_mismatch_refuses_session_before_upstream(voice_setup, monkeypatch):
    store, private, client = voice_setup
    enable(private)
    entry = {"entry_id": "one", "speaker": "member", "text": "private", "at": store.now()}
    assert (
        client.post(
            f"/api/agent-threads/{THREAD_ID}/voice-entries", json={"entries": [entry]}
        ).status_code
        == 200
    )
    path = store.path.parent / "agent-threads" / private.member_id / f"{THREAD_ID}.jsonl"
    record = json.loads(path.read_text())
    record["privateOwner"] = "other"
    path.write_text(json.dumps(record) + "\n")
    calls = []
    mock_transport(monkeypatch, lambda request: calls.append(request) or reply({}))
    response = client.post("/api/voice/sessions", json=OFFER)
    assert response.status_code == 409
    assert calls == []


def test_project_voice_entries_publish_and_memory_is_owner_bound(manifest, tmp_path):
    from tests.helpers import create_named_app, signed_in_client

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    user_id = store.local_owner.user_id
    project_id = app.state.default_project_id
    with store.connection() as db:
        db.execute(
            "INSERT INTO agent_threads (thread_id,user_id,kind,project_id,created_at) VALUES (?,?,?,?,?)",
            (THREAD_ID, user_id, "project", project_id, store.now()),
        )
    root = store.path.parent / "memory" / project_id
    for folder, text in [
        ("shared", "shared"),
        (f"members/{user_id}", "mine"),
        ("members/other", "hidden"),
    ]:
        path = root / folder
        path.mkdir(parents=True)
        (path / "MEMORY.md").write_text(text)
    with signed_in_client(app) as client:
        path = f"/api/agent-threads/{THREAD_ID}"
        body = {
            "entries": [
                {"entry_id": "first", "speaker": "member", "text": "spoken", "at": store.now()}
            ]
        }
        assert client.post(path + "/voice-entries", json=body).json() == {"appended": 1}
        assert client.post(path + "/voice-entries", json=body).json() == {"appended": 0}
        assert client.get(path + "/memory").json() == {
            "shared_index": "shared",
            "private_index": "mine",
            "truncated": False,
        }
        service = app.state.catalog.open(project_id)
        transcript = (
            service.history.workspace.root / "chat" / f"private-{user_id}-{THREAD_ID}.jsonl"
        )
        record = json.loads(transcript.read_text())
        assert record["source"] == "voice" and record["graphTarget"]["kind"] == "main"
        with store.connection() as db:
            db.execute(
                "UPDATE agent_threads SET closed_at=? WHERE thread_id=?", (store.now(), THREAD_ID)
            )
        assert client.post(path + "/voice-entries", json=body).status_code == 404


@pytest.mark.parametrize("bad_record", [None, [], 42, "not a record"])
def test_non_object_thread_record_is_refused(voice_setup, monkeypatch, bad_record):
    store, private, client = voice_setup
    enable(private)
    path = store.path.parent / "agent-threads" / private.member_id / f"{THREAD_ID}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bad_record) + "\n")
    calls = []
    mock_transport(monkeypatch, lambda request: calls.append(request) or reply({}))
    response = client.post("/api/voice/sessions", json=OFFER)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "thread_transcript_invalid"
    assert calls == []


@pytest.mark.parametrize("closed_at", ["", "2026-01-01T00:00:00+00:00"])
def test_closed_thread_is_hidden_from_voice(voice_setup, closed_at):
    store, private, client = voice_setup
    with store.connection() as connection:
        connection.execute(
            "UPDATE agent_threads SET closed_at=? WHERE thread_id=?", (closed_at, THREAD_ID)
        )
    assert client.get(f"/api/agent-threads/{THREAD_ID}/memory").status_code == 404
    assert (
        client.post(
            f"/api/agent-threads/{THREAD_ID}/voice-entries", json={"entries": []}
        ).status_code
        == 404
    )


def test_space_voice_append_shares_the_turn_transcript_lock(voice_setup):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event

    from rcp.runs.space_chat import space_transcript_lock
    from rcp.service import read_space_chat_transcript
    from rcp.voice_threads import VoiceEntry, append_space_entries, owned_thread, transcript_path

    store, private, _ = voice_setup
    thread = owned_thread(store, THREAD_ID, private.member_id)
    path = transcript_path(store.path.parent, thread)
    entry = VoiceEntry(entry_id="first", speaker="member", text="Hello", at=store.now())
    started = Event()

    def append():
        started.set()
        return append_space_entries(store, path, thread, [entry])

    with ThreadPoolExecutor(max_workers=1) as executor:
        with space_transcript_lock(store, THREAD_ID):
            future = executor.submit(append)
            assert started.wait(5)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.05)
            assert not path.exists()
        assert future.result(timeout=5) == 1
    assert append_space_entries(store, path, thread, [entry]) == 0
    transcript = read_space_chat_transcript(store.path.parent, private.member_id, THREAD_ID)
    assert transcript is not None
    assert [(message.text, message.source) for message in transcript.messages] == [
        ("Hello", "voice")
    ]
