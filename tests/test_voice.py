from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from rcp import limits, transcription, voice
from rcp.service_connections import ConnectionError, VoiceSettings

from .test_service_connections import KEY, MIME, connection, mock_transport, setup  # noqa: F401

OFFER = {"sdp_offer": "v=0\r\n", "tools": [], "playbook": "How RCP works."}
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


def test_session_contract_and_private_session_allocation(voice_setup, monkeypatch):
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
    record = response.json()["session"]
    assert record["member_id"] == private.member_id
    assert record["revision"] == 0 and record["generation"]
    assert record["entries"] == record["receipts"] == []
    assert private._voice_path(record["id"]).stat().st_mode & 0o777 == 0o600


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


def transcript_save(record, **changes):
    return {
        "member_id": record["member_id"],
        "generation": record["generation"],
        "revision": record["revision"] + 1,
        "entries": [{"speaker": "member", "text": "Earlier topic"}],
        "receipts": [],
        **changes,
    }


def test_transcript_generation_revision_delete_and_member_fences(voice_setup, monkeypatch):
    _, private, client = voice_setup
    enable(private)
    mock_transport(monkeypatch, lambda _: reply({"transport": {"sdp": "answer"}}))
    record = client.post("/api/voice/sessions", json=OFFER).json()["session"]
    path = f"/api/voice/sessions/{record['id']}"
    body = transcript_save(record)
    assert client.put(path, json=body).status_code == 200
    assert client.put(path, json=body).json()["detail"]["code"] == "voice_session_stale"
    resumed = client.post("/api/voice/sessions", json={**OFFER, "resume_id": record["id"]}).json()[
        "session"
    ]
    assert resumed["generation"] != record["generation"] and resumed["revision"] == 1
    assert (
        client.put(path, json={**body, "revision": 2}).json()["detail"]["code"]
        == "voice_session_superseded"
    )
    assert client.put(path, json=transcript_save(resumed, member_id="other")).status_code == 403
    assert client.put(path, json=transcript_save(resumed, ended=True)).status_code == 200
    assert client.put(path, json=transcript_save(resumed, revision=3)).status_code == 409
    assert client.delete(path).status_code == 200
    assert client.put(path, json=transcript_save(resumed, revision=4)).status_code == 404
    assert (
        client.post("/api/voice/sessions", json={**OFFER, "resume_id": record["id"]}).status_code
        == 404
    )


def test_failed_upstream_leaves_live_generation_and_history_unchanged(voice_setup, monkeypatch):
    _, private, client = voice_setup
    enable(private)
    records = [private.claim_voice_session() for _ in range(limits.VOICE_TRANSCRIPT_MAX_SESSIONS)]
    record = records[-1]
    before = {path: path.read_bytes() for path in private.root.rglob("*.json")}
    mock_transport(monkeypatch, lambda _: reply({}, status=503))
    assert (
        client.post("/api/voice/sessions", json={**OFFER, "resume_id": record["id"]}).status_code
        == 502
    )
    assert client.post("/api/voice/sessions", json=OFFER).status_code == 502
    assert {path: path.read_bytes() for path in private.root.rglob("*.json")} == before
    assert (
        client.put(f"/api/voice/sessions/{record['id']}", json=transcript_save(record)).status_code
        == 200
    )


def test_save_generation_check_and_delete_do_not_scan_history(voice_setup, monkeypatch):
    _, private, client = voice_setup
    record = private.claim_voice_session()
    path = f"/api/voice/sessions/{record['id']}"

    def unexpected_scan(_self):
        pytest.fail("Single-record operations must not scan or prune history")

    monkeypatch.setattr(type(private), "_voice_records", unexpected_scan)
    assert client.put(path, json=transcript_save(record)).status_code == 200
    assert client.get(f"{path}/generation").json() == {"generation": record["generation"]}
    assert client.delete(path).status_code == 200
    assert client.get(f"{path}/generation").status_code == 404


# A fixed key keeps test ids identical across xdist workers.
@pytest.mark.parametrize("request_id", [None, "5b0e7c1e-3f4a-4d2b-9c8e-1a2b3c4d5e6f"])
def test_resume_sends_only_bounded_historical_speech_and_returns_receipts(
    voice_setup, monkeypatch, request_id
):
    _, private, client = voice_setup
    enable(private)
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return reply({"transport": {"sdp": "answer"}})

    mock_transport(monkeypatch, handler)
    record = client.post("/api/voice/sessions", json=OFFER).json()["session"]
    entries = [
        {
            "speaker": "member" if i % 2 else "agent",
            "text": str(i),
            "provider_order": str(i),
            "source": "project:example" if i % 2 == 0 else None,
        }
        for i in range(150)
    ]
    receipt = {
        "tool": "rcp_chat",
        "call_id": "call-1",
        "argument_fingerprint": "a" * 64,
        "target": {
            "project_id": "project",
            "project_name": "Project",
            "graph_target": {"kind": "main", "branch_id": None},
        },
        "outcome": "unknown",
        "task_id": "task-1",
        "episode_id": None,
    }
    if request_id is not None:
        receipt["request_id"] = request_id
    saved = client.put(
        f"/api/voice/sessions/{record['id']}",
        json=transcript_save(record, entries=entries, receipts=[receipt]),
    )
    assert saved.status_code == 200
    resumed = client.post("/api/voice/sessions", json={**OFFER, "resume_id": record["id"]}).json()
    assert resumed["input_truncated"]
    assert resumed["session"]["receipts"] == [{**receipt, "request_id": request_id}]
    inputs = payloads[-1]["session"]["input"]
    assert 0 < len(inputs) <= limits.VOICE_RESUME_MAX_MESSAGES
    assert {item["role"] for item in inputs} == {"user", "assistant"}
    assert {(item["type"], item["role"], item["content"][0]["type"]) for item in inputs} == {
        ("message", "user", "input_text"),
        ("message", "assistant", "output_text"),
    }
    assert inputs[-1]["content"][0]["text"].endswith(entries[-1]["text"])
    assert entries[-2]["source"] in inputs[-2]["content"][0]["text"]
    assert payloads[-1]["session"]["store"] is False
    from rcp.service_connections import voice_resume_input

    inputs, truncated = voice_resume_input([{**entry, "text": "漢" * 4000} for entry in entries])
    assert truncated and inputs
    assert (
        sum(
            len(item["content"][0]["text"].encode()) + limits.VOICE_RESUME_MESSAGE_OVERHEAD_TOKENS
            for item in inputs
        )
        <= limits.VOICE_RESUME_MAX_TOKENS
    )

    newest = {**entries[0], "text": "\\" * (limits.VOICE_TRANSCRIPT_ENTRY_MAX_BYTES - 4) + "tail"}
    clipped, truncated = voice_resume_input([newest])
    assert truncated and len(clipped) == 1
    assert clipped[0]["role"] == "assistant"
    assert newest["source"] in clipped[0]["content"][0]["text"]
    assert clipped[0]["content"][0]["text"].endswith("tail")
    assert (
        len(clipped[0]["content"][0]["text"].encode()) + limits.VOICE_RESUME_MESSAGE_OVERHEAD_TOKENS
        <= limits.VOICE_RESUME_MAX_TOKENS
    )


def test_transcript_metadata_retention_and_disconnect_removal(voice_setup, monkeypatch):
    _, private, client = voice_setup
    connection = enable(private)
    clock = [10000000.0]
    monkeypatch.setattr("rcp.service_connections.time.time", lambda: clock[0])
    records = []
    for _ in range(limits.VOICE_TRANSCRIPT_MAX_SESSIONS + 1):
        records.append(private.claim_voice_session())
        clock[0] += 1
    assert not private._voice_path(records[0]["id"]).exists()
    page = client.get("/api/voice/sessions?offset=0&limit=5").json()
    assert len(page["sessions"]) == 5 and page["next_offset"] == 5
    assert page["sessions"][0]["id"] == records[-1]["id"]
    assert all("entries" not in row and "receipts" not in row for row in page["sessions"])
    private.disconnect(connection["id"])
    assert len(private.list_voice_sessions(0, 20)["sessions"]) == 20
    clock[0] += limits.VOICE_TRANSCRIPT_RETENTION_SECONDS - 2
    private.claim_voice_session(records[-1]["id"])
    clock[0] += 2
    assert [row["id"] for row in private.list_voice_sessions(0, 20)["sessions"]] == [
        records[-1]["id"]
    ]
    clock[0] += limits.VOICE_TRANSCRIPT_RETENTION_SECONDS
    # Knowing an expired id does not revive it.
    with pytest.raises(ConnectionError):
        private.claim_voice_session(records[-1]["id"])
    assert private.list_voice_sessions(0, 20)["sessions"] == []
    record = private.claim_voice_session()
    private.remove_member_data()
    assert not private._voice_path(record["id"]).exists()


def test_transcript_request_entry_and_session_byte_bounds(voice_setup, monkeypatch):
    _, private, client = voice_setup
    record = private.claim_voice_session()
    path = f"/api/voice/sessions/{record['id']}"
    # A chunked body declares no length, so it is refused before it is buffered.
    chunked = client.put(path, content=iter([b"{}"]), headers={"content-type": "application/json"})
    assert chunked.status_code == 411
    oversized = "漢" * (limits.VOICE_TRANSCRIPT_ENTRY_MAX_BYTES // 3 + 1)
    assert (
        client.put(
            path, json=transcript_save(record, entries=[{"speaker": "member", "text": oversized}])
        ).status_code
        == 422
    )
    monkeypatch.setattr(limits, "VOICE_TRANSCRIPT_SESSION_MAX_BYTES", 100)
    assert client.put(path, json=transcript_save(record)).status_code == 413
    monkeypatch.setattr(limits, "VOICE_TRANSCRIPT_REQUEST_MAX_BYTES", 50)
    assert client.put(path, json=transcript_save(record)).status_code == 413
    assert private._voice_record(record["id"])["revision"] == 0
