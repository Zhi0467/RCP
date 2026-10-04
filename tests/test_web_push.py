from __future__ import annotations

import json

import http_ece
import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from rcp import web_push
from rcp.api import create_app
from rcp.storage import AppStore
from rcp.web_push import Subscription, VapidKey, WebPushRefused, b64url_decode, b64url_encode
from tests.helpers import signed_in_client

# RFC 8291 section 5 and appendix A.
_RFC_UA_PUBLIC = (
    "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
)
_RFC_AUTH = "BTBZMqHH6r4Tts7J_aSIgg"
_RFC_AS_PRIVATE = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
_RFC_SALT = "DGv6ra1nlYgDCS1FRnbzlw"
_RFC_BODY = (
    "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYW"
    "AmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxs"
    "j_Qulcy4a-fN"
)
_PUBLIC_ADDRESS = "17.253.144.10"


def _receiver():
    private = ec.generate_private_key(ec.SECP256R1())
    public = web_push._public_bytes(private.public_key())
    return private, b64url_encode(public), b64url_encode(b"a" * 16)


def _subscription(endpoint="https://web.push.apple.com/abc", origin="https://rcp.example.com"):
    _, p256dh, auth = _receiver()
    return Subscription(endpoint=endpoint, p256dh=p256dh, auth=auth, origin=origin)


def test_encryption_matches_rfc_vector_and_an_independent_decoder() -> None:
    as_private = ec.derive_private_key(
        int.from_bytes(b64url_decode(_RFC_AS_PRIVATE), "big"), ec.SECP256R1()
    )
    body = web_push.encrypt(
        b"When I grow up, I want to be a watermelon",
        ua_public=b64url_decode(_RFC_UA_PUBLIC),
        auth_secret=b64url_decode(_RFC_AUTH),
        as_private=as_private,
        salt=b64url_decode(_RFC_SALT),
    )
    assert b64url_encode(body) == _RFC_BODY

    private, p256dh, auth = _receiver()
    body = web_push.encrypt(
        b'{"reason":"proposal"}', ua_public=b64url_decode(p256dh), auth_secret=b64url_decode(auth)
    )
    decoded = http_ece.decrypt(
        body, private_key=private, auth_secret=b64url_decode(auth), version="aes128gcm"
    )
    assert decoded == b'{"reason":"proposal"}'
    with pytest.raises(ValueError):
        web_push.encrypt(
            b"x" * (web_push.MAX_PLAINTEXT_BYTES + 1),
            ua_public=b64url_decode(p256dh),
            auth_secret=b64url_decode(auth),
        )


def test_vapid_token_verifies_with_its_public_key() -> None:
    key = VapidKey.from_pem(VapidKey.generate().to_pem())
    value = key.authorization("https://web.push.apple.com", "https://rcp.example.com", now=1000)
    token, public = value.removeprefix("vapid t=").split(", k=")
    header, claims, signature = token.split(".")
    assert json.loads(b64url_decode(claims))["sub"] == "https://rcp.example.com"
    raw = b64url_decode(signature)
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64url_decode(public)).verify(
        encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
        f"{header}.{claims}".encode(),
        ec.ECDSA(hashes.SHA256()),
    )


@pytest.mark.parametrize(
    ("endpoint", "origin", "addresses", "resolves"),
    [
        ("http://web.push.apple.com/abc", "https://rcp.example.com", [_PUBLIC_ADDRESS], False),
        ("https://push.attacker.example/abc", "https://rcp.example.com", [_PUBLIC_ADDRESS], False),
        (
            "https://web.push.apple.com:8443/abc",
            "https://rcp.example.com",
            [_PUBLIC_ADDRESS],
            False,
        ),
        (
            "https://web.push.apple.com/" + "a" * 4096,
            "https://rcp.example.com",
            [_PUBLIC_ADDRESS],
            False,
        ),
        ("https://web.push.apple.com/abc", "http://rcp.example.com", [_PUBLIC_ADDRESS], False),
        ("https://web.push.apple.com/abc", "https://rcp.example.com", ["10.0.0.5"], True),
        (
            "https://web.push.apple.com/abc",
            "https://rcp.example.com",
            [_PUBLIC_ADDRESS, "127.0.0.1"],
            True,
        ),
    ],
)
def test_refused_destinations_open_no_connection(endpoint, origin, addresses, resolves) -> None:
    resolved = []
    requests = []

    def resolve(host):
        resolved.append(host)
        return addresses

    with pytest.raises(WebPushRefused):
        web_push.send(
            _subscription(endpoint, origin),
            {"reason": "proposal"},
            key=VapidKey.generate(),
            topic="t",
            ttl_seconds=60,
            resolve=resolve,
            transport=httpx.MockTransport(lambda request: requests.append(request)),
        )
    assert bool(resolved) is resolves
    assert requests == []


@pytest.mark.parametrize(
    ("status", "headers", "outcome", "retry_after"),
    [
        (201, {}, "posted", None),
        (410, {}, "gone", None),
        (429, {"Retry-After": "30"}, "retry", 30.0),
        (429, {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}, "retry", 0.0),
        (503, {}, "retry", None),
        (400, {}, "failed", None),
        (301, {"Location": "https://10.0.0.5/"}, "failed", None),
    ],
)
def test_send_outcomes_pin_the_checked_address(status, headers, outcome, retry_after) -> None:
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers=headers)

    result = web_push.send(
        _subscription(),
        {"reason": "proposal"},
        key=VapidKey.generate(),
        topic="0123abcd",
        ttl_seconds=60,
        resolve=lambda _host: [_PUBLIC_ADDRESS],
        transport=httpx.MockTransport(handler),
    )
    assert (result.outcome, result.retry_after_seconds) == (outcome, retry_after)
    [request] = requests
    assert request.url.host == _PUBLIC_ADDRESS
    assert request.headers["host"] == "web.push.apple.com"
    assert request.extensions["sni_hostname"] == "web.push.apple.com"
    assert (request.headers["ttl"], request.headers["topic"]) == ("60", "0123abcd")


def test_team_phone_registers_receives_and_detaches(manifest, tmp_path) -> None:
    store, bootstrap = AppStore.initialize_team_space(tmp_path / "rcp.sqlite3", "Lab")
    _member, token = store.enroll_team_member(bootstrap, "Member")
    app = create_app(str(manifest.path), data_dir=tmp_path)
    sender = app.state.notification_sender
    sender.resolve = lambda _host: [_PUBLIC_ADDRESS]
    sent = []
    sender.transport = httpx.MockTransport(
        lambda request: sent.append(request) or httpx.Response(201)
    )
    client = signed_in_client(app, base_url="https://testserver")
    client.post("/api/team/session/exchange", json={"token": token})
    origin = {"Origin": "https://testserver"}
    key = client.get("/api/notifications/web-push/key").json()["application_server_key"]
    assert len(b64url_decode(key)) == 65
    _, p256dh, auth = _receiver()
    registration = {
        "endpoint": "https://web.push.apple.com/abc",
        "keys": {"p256dh": p256dh, "auth": auth},
    }
    refused = {**registration, "endpoint": "https://push.attacker.example/abc"}
    assert (
        client.post("/api/notifications/devices/web-push", json=refused, headers=origin).status_code
        == 422
    )
    response = client.post("/api/notifications/devices/web-push", json=registration, headers=origin)
    assert response.status_code == 200
    device_id = response.json()["device_id"]
    assert client.post(
        f"/api/notifications/devices/{device_id}/test", json={}, headers=origin
    ).json() == {"outcome": "posted"}
    assert len(sent) == 1
    # A revisit keeps the device; a new endpoint from the session replaces it.
    same = client.post("/api/notifications/devices/web-push", json=registration, headers=origin)
    assert same.json()["device_id"] == device_id
    again = client.post(
        "/api/notifications/devices/web-push",
        json={**registration, "endpoint": "https://web.push.apple.com/renewed"},
        headers=origin,
    )
    assert again.json()["device_id"] != device_id
    [listed] = client.get("/api/notifications/devices").json()
    assert (listed["device_id"], listed["kind"], listed["status"]) == (
        again.json()["device_id"],
        "web_push",
        "on",
    )
    assert [item["kind"] for item in store.notification_devices()] == ["web_push"]
    assert client.post("/api/team/session/logout", json={}, headers=origin).status_code == 200
    assert store.notification_devices() == []
    assert store.web_push_subscription(again.json()["device_id"]) is None
    # The key survives the device and is reused, not regenerated.
    assert store.notification_vapid_key() == store.notification_vapid_key()


def test_personal_owner_cannot_register_a_phone_on_the_owner_api(manifest, tmp_path) -> None:
    from .helpers import create_named_app

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = signed_in_client(app)
    _, p256dh, auth = _receiver()
    response = client.post(
        "/api/notifications/devices/web-push",
        json={
            "endpoint": "https://web.push.apple.com/abc",
            "keys": {"p256dh": p256dh, "auth": auth},
        },
    )
    assert response.status_code == 403


@pytest.mark.parametrize("status", [201, 410])
def test_sender_delivers_queued_items_and_drops_gone_phones(manifest, tmp_path, status) -> None:
    from .helpers import create_named_app
    from .test_notifications import _append, _blocker_patch

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    sender = app.state.notification_sender
    sender.resolve = lambda _host: [_PUBLIC_ADDRESS]
    bodies = []
    sender.transport = httpx.MockTransport(
        lambda request: bodies.append(request.content) or httpx.Response(status)
    )
    private, p256dh, auth = _receiver()
    store.notification_vapid_key()
    device = store.register_web_push_device(
        store.local_owner.user_id,
        session_id=None,
        endpoint="https://web.push.apple.com/abc",
        p256dh=p256dh,
        auth=auth,
        origin="https://rcp.example.com",
        opens_links=False,
    )
    sender.run_pass()
    _append(app, app.state.catalog.open(app.state.default_project_id), _blocker_patch("blk/new"))
    sender.run_pass()
    [body] = bodies
    payload = json.loads(
        http_ece.decrypt(
            body, private_key=private, auth_secret=b64url_decode(auth), version="aes128gcm"
        )
    )
    # A notify-only phone has no read access, so it gets no link.
    assert set(payload) == {"notification_id", "reason", "project_name"}
    if status == 201:
        assert [row["last_status"] for row in store.notification_outbox()] == ["posted"]
        sender.run_pass()
        assert len(bodies) == 1
    else:
        assert store.notification_device(device["device_id"]) is None
        assert store.notification_outbox() == []


def _phone_sender(manifest, tmp_path):
    from .helpers import create_named_app

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    sender = app.state.notification_sender
    _, p256dh, auth = _receiver()
    store.notification_vapid_key()
    device = store.register_web_push_device(
        store.local_owner.user_id,
        session_id=None,
        endpoint="https://web.push.apple.com/abc",
        p256dh=p256dh,
        auth=auth,
        origin="https://rcp.example.com",
        opens_links=False,
    )
    return app, store, sender, device


def test_removing_a_phone_mid_pass_stops_later_sends(manifest, tmp_path) -> None:
    from .test_notifications import _append, _blocker_patch

    app, store, sender, device = _phone_sender(manifest, tmp_path)
    sender.resolve = lambda _host: [_PUBLIC_ADDRESS]
    requests = []

    def remove_on_first(request):
        requests.append(request)
        store.delete_notification_device(device["device_id"])
        return httpx.Response(201)

    sender.transport = httpx.MockTransport(remove_on_first)
    sender.run_pass()
    service = app.state.catalog.open(app.state.default_project_id)
    _append(app, service, _blocker_patch("blk/one"))
    _append(app, service, _blocker_patch("blk/two"))
    sender.run_pass()
    assert len(requests) == 1


def test_a_resolver_outage_keeps_the_item_for_retry(manifest, tmp_path) -> None:
    from .test_notifications import _append, _blocker_patch

    app, store, sender, device = _phone_sender(manifest, tmp_path)
    requests = []
    sender.transport = httpx.MockTransport(
        lambda request: requests.append(request) or httpx.Response(201)
    )

    def outage(_host):
        raise OSError("temporary failure in name resolution")

    sender.resolve = outage
    sender.run_pass()
    _append(app, app.state.catalog.open(app.state.default_project_id), _blocker_patch("blk/dns"))
    sender.run_pass()
    assert requests == []
    [row] = store.notification_outbox()
    assert row["last_status"] != "posted"
    assert store.notification_device(device["device_id"])["last_status"] == "on"
    sender.resolve = lambda _host: [_PUBLIC_ADDRESS]
    with store.connection() as connection:
        connection.execute("UPDATE notification_outbox SET next_attempt_at=created_at")
    sender.run_pass()
    assert len(requests) == 1


def test_a_pass_sends_new_attention_after_reconciling_it(manifest, tmp_path) -> None:
    from .test_notifications import _append, _blocker_patch

    app, store, sender, _device = _phone_sender(manifest, tmp_path)
    sender.resolve = lambda _host: [_PUBLIC_ADDRESS]
    requests = []
    sender.transport = httpx.MockTransport(
        lambda request: requests.append(request) or httpx.Response(201)
    )
    sender.run_pass()
    assert requests == []
    _append(app, app.state.catalog.open(app.state.default_project_id), _blocker_patch("blk/boot"))
    sender.run_pass()
    assert len(store.notification_outbox()) == 1
    assert len(requests) == 1
