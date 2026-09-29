"""Web Push encryption (RFC 8291), VAPID signing (RFC 8292), and bounded sends.

A subscription endpoint is a destination the server posts to on a device's
behalf, so it is validated at registration and again before every send.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import socket
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Literal
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hmac import HMAC
from pydantic import BaseModel, ConfigDict, Field

from rcp.limits import (
    WEB_PUSH_CONNECT_TIMEOUT_SECONDS,
    WEB_PUSH_MAX_ENDPOINT_BYTES,
    WEB_PUSH_MAX_ORIGIN_BYTES,
    WEB_PUSH_REQUEST_TIMEOUT_SECONDS,
    WEB_PUSH_VAPID_LIFETIME_SECONDS,
)

RECORD_SIZE = 4096
_TAG_BYTES = 16
_KEY_BYTES = 65
_AUTH_SECRET_BYTES = 16
# One record holds the whole message: the record size minus the padding
# delimiter and the AEAD tag.
MAX_PLAINTEXT_BYTES = RECORD_SIZE - _TAG_BYTES - 1

# The browser push services RCP will post to. Anything else is refused before
# any DNS lookup or connection.
PUSH_SERVICE_HOSTS = frozenset(
    {"web.push.apple.com", "fcm.googleapis.com", "updates.push.services.mozilla.com"}
)
PUSH_SERVICE_HOST_SUFFIXES = (".notify.windows.com",)


class WebPushRefused(ValueError):
    """A subscription RCP will not send to; no connection was opened."""


class WebPushUnavailable(OSError):
    """The push service could not be resolved now; retry later."""


def b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _hmac_sha256(key: bytes, data: bytes) -> bytes:
    mac = HMAC(key, hashes.SHA256())
    mac.update(data)
    return mac.finalize()


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    # One HKDF-Expand block covers every length used here (<= 32).
    prk = _hmac_sha256(salt, ikm)
    return _hmac_sha256(prk, info + b"\x01")[:length]


def _public_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def _load_public(raw: bytes) -> ec.EllipticCurvePublicKey:
    if len(raw) != _KEY_BYTES:
        raise WebPushRefused("a Web Push public key is 65 bytes")
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    except ValueError as exc:
        raise WebPushRefused("the Web Push public key is not a P-256 point") from exc


def encrypt(
    plaintext: bytes,
    *,
    ua_public: bytes,
    auth_secret: bytes,
    as_private: ec.EllipticCurvePrivateKey | None = None,
    salt: bytes | None = None,
) -> bytes:
    """Return an ``aes128gcm`` body for one subscription.

    ``as_private`` and ``salt`` are fresh per message; tests pass the RFC
    values to reproduce its example.
    """
    if len(plaintext) > MAX_PLAINTEXT_BYTES:
        raise ValueError("Web Push plaintext exceeds one record")
    if len(auth_secret) != _AUTH_SECRET_BYTES:
        raise WebPushRefused("a Web Push auth secret is 16 bytes")
    receiver = _load_public(ua_public)
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    salt = salt or os.urandom(16)
    as_public = _public_bytes(as_private.public_key())
    ecdh_secret = as_private.exchange(ec.ECDH(), receiver)
    key_info = b"WebPush: info\x00" + ua_public + as_public
    ikm = _hkdf(auth_secret, ecdh_secret, key_info, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    ciphertext = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)
    header = salt + struct.pack("!IB", RECORD_SIZE, len(as_public)) + as_public
    return header + ciphertext


@dataclass(frozen=True)
class VapidKey:
    """The server's VAPID signing key; separate from per-message ECDH keys."""

    private_key: ec.EllipticCurvePrivateKey

    @classmethod
    def generate(cls) -> VapidKey:
        return cls(ec.generate_private_key(ec.SECP256R1()))

    @classmethod
    def from_pem(cls, pem: str) -> VapidKey:
        key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
            raise ValueError("the VAPID key must be a P-256 private key")
        return cls(key)

    def to_pem(self) -> str:
        return self.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii")

    @property
    def application_server_key(self) -> str:
        """The base64url public key a browser passes to ``subscribe``."""
        return b64url_encode(_public_bytes(self.private_key.public_key()))

    def authorization(self, audience: str, subject: str, *, now: float | None = None) -> str:
        """Return the ``Authorization`` value for one push-service origin."""
        issued = int(time.time() if now is None else now)
        header = b64url_encode(
            json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode()
        )
        claims = b64url_encode(
            json.dumps(
                {"aud": audience, "exp": issued + WEB_PUSH_VAPID_LIFETIME_SECONDS, "sub": subject},
                separators=(",", ":"),
            ).encode()
        )
        signing_input = f"{header}.{claims}".encode("ascii")
        r, s = decode_dss_signature(self.private_key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
        signature = b64url_encode(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        return f"vapid t={header}.{claims}.{signature}, k={self.application_server_key}"


@dataclass(frozen=True)
class Subscription:
    endpoint: str
    p256dh: str
    auth: str
    # The HTTPS origin the device registered from. Apple rejects a VAPID
    # subject that is not a real ``https:`` or ``mailto:`` URI.
    origin: str


class WebPushKeys(BaseModel):
    """The ``keys`` member of ``PushSubscription.toJSON()``."""

    model_config = ConfigDict(extra="forbid")

    p256dh: str = Field(min_length=1, max_length=128)
    auth: str = Field(min_length=1, max_length=64)


Resolver = Callable[[str], list[str]]


def resolve_host(host: str) -> list[str]:
    return sorted({info[4][0] for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})


def _allowed_host(host: str) -> bool:
    return host in PUSH_SERVICE_HOSTS or any(
        host.endswith(suffix) and len(host) > len(suffix) for suffix in PUSH_SERVICE_HOST_SUFFIXES
    )


def validate_subscription(subscription: Subscription, *, resolve: Resolver = resolve_host) -> str:
    """Refuse an unsafe subscription and return the public address to connect to."""
    if len(subscription.endpoint.encode()) > WEB_PUSH_MAX_ENDPOINT_BYTES:
        raise WebPushRefused("the push endpoint is too long")
    origin = urlsplit(subscription.origin)
    if (
        len(subscription.origin.encode()) > WEB_PUSH_MAX_ORIGIN_BYTES
        or origin.scheme != "https"
        or not origin.hostname
        or origin.path not in ("", "/")
        or origin.query
        or origin.username is not None
    ):
        raise WebPushRefused("the registering origin must be an HTTPS origin")
    try:
        _load_public(b64url_decode(subscription.p256dh))
        auth = b64url_decode(subscription.auth)
    except (ValueError, TypeError) as exc:
        raise WebPushRefused("the subscription keys are not base64url") from exc
    if len(auth) != _AUTH_SECRET_BYTES:
        raise WebPushRefused("a Web Push auth secret is 16 bytes")
    endpoint = urlsplit(subscription.endpoint)
    host = (endpoint.hostname or "").lower()
    try:
        port = endpoint.port
    except ValueError as exc:
        raise WebPushRefused("the push endpoint port is invalid") from exc
    if (
        endpoint.scheme != "https"
        or endpoint.username is not None
        or endpoint.password is not None
        or port not in (None, 443)
        or not _allowed_host(host)
    ):
        raise WebPushRefused("the push endpoint is not an allowed push service")
    try:
        addresses = resolve(host)
    except OSError as exc:
        # A resolver outage is temporary; only an unsafe answer is refused.
        raise WebPushUnavailable("the push service did not resolve") from exc
    if not addresses or not all(ipaddress.ip_address(item).is_global for item in addresses):
        raise WebPushRefused("the push service resolved to a non-public address")
    return addresses[0]


SendOutcome = Literal["posted", "gone", "retry", "failed"]


@dataclass(frozen=True)
class SendResult:
    outcome: SendOutcome
    retry_after_seconds: float | None = None


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


def send(
    subscription: Subscription,
    payload: dict[str, object],
    *,
    key: VapidKey,
    topic: str,
    ttl_seconds: int,
    resolve: Resolver = resolve_host,
    transport: httpx.BaseTransport | None = None,
) -> SendResult:
    """Post one encrypted message; redirects are never followed."""
    try:
        address = validate_subscription(subscription, resolve=resolve)
    except WebPushUnavailable:
        return SendResult("retry")
    endpoint = urlsplit(subscription.endpoint)
    host = endpoint.hostname or ""
    body = encrypt(
        json.dumps(payload, separators=(",", ":")).encode(),
        ua_public=b64url_decode(subscription.p256dh),
        auth_secret=b64url_decode(subscription.auth),
    )
    pinned = f"[{address}]" if ":" in address else address
    # Connect to the address that was checked; TLS still verifies the name.
    url = endpoint._replace(netloc=pinned).geturl()
    headers = {
        "Host": host,
        "TTL": str(max(0, ttl_seconds)),
        "Topic": topic,
        "Urgency": "normal",
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "Authorization": key.authorization(f"https://{host}", subscription.origin),
    }
    timeout = httpx.Timeout(
        WEB_PUSH_REQUEST_TIMEOUT_SECONDS, connect=WEB_PUSH_CONNECT_TIMEOUT_SECONDS
    )
    try:
        with httpx.Client(
            timeout=timeout, follow_redirects=False, trust_env=False, transport=transport
        ) as client:
            response = client.post(
                url, content=body, headers=headers, extensions={"sni_hostname": host}
            )
    except httpx.HTTPError:
        return SendResult("retry")
    if response.status_code in (200, 201, 202):
        return SendResult("posted")
    if response.status_code in (404, 410):
        return SendResult("gone")
    if response.status_code == 429 or response.status_code >= 500:
        return SendResult("retry", _retry_after(response.headers.get("Retry-After")))
    return SendResult("failed")
