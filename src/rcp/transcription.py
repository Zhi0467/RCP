"""Bounded in-memory transcription adapters and browser-format connection probes."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import re
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from rcp import limits
from rcp.service_connections import ConnectionError

FORMATS = {"audio/webm;codecs=opus": "webm", "audio/mp4;codecs=mp4a.40.2": "mp4"}
PRESETS = {
    "openai": ("https://api.openai.com/v1", "gpt-transcribe", "OpenAI"),
    "groq": ("https://api.groq.com/openai/v1", "whisper-large-v3-turbo", "Groq"),
}
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_MODEL = "gemini-3.5-transcribe"


class ConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: str
    preset: str | None = None
    base_url: str | None = None
    model: str | None = Field(default=None, max_length=200)
    key: SecretStr = Field(default=SecretStr(""), max_length=limits.TRANSCRIPTION_KEY_MAX_CHARS)

    def configuration(self) -> dict:
        key = self.key.get_secret_value()
        if self.kind == "gemini" and self.preset is None and self.base_url is None:
            base, model, label = GEMINI_URL, GEMINI_MODEL, "Gemini"
        elif self.kind == "openai_compatible" and self.preset in PRESETS and self.base_url is None:
            base, model, label = PRESETS[self.preset]
        elif self.kind == "openai_compatible" and self.preset == "custom" and self.base_url:
            base, model, label = self.base_url, "", "Custom server"
        else:
            raise ConnectionError("address_not_allowed", 422)
        base = checked_address(base)
        model = self.model or model
        if not re.fullmatch(r"[A-Za-z0-9._:/-]+", model) or (not key and self.preset != "custom"):
            raise ConnectionError("connection_check_failed", 422, "Invalid service configuration.")
        # A secret must not be copied into public metadata or an HTTP request URL.
        if key and any(key in value for value in (base, model, label)):
            raise ConnectionError("connection_check_failed", 422, "Invalid service configuration.")
        return dict(
            id=uuid.uuid4().hex,
            kind=self.kind,
            preset=self.preset,
            base_url=base,
            model=model,
            label=label,
        )


def checked_address(base: str) -> str:
    try:
        url = urlsplit(base)
        host = url.hostname
        _ = url.port
        if (
            not host
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or "?" in base
            or "#" in base
            or any(c.isspace() or ord(c) < 32 for c in base)
            or "\\" in base
        ):
            raise ValueError
        loopback = host == "localhost"
        with suppress(ValueError):
            loopback = loopback or ipaddress.ip_address(host).is_loopback
        if url.scheme != "https" and not (url.scheme == "http" and loopback):
            raise ValueError
        return str(httpx.URL(base)).rstrip("/")
    except (ValueError, httpx.InvalidURL):
        raise ConnectionError("address_not_allowed", 422) from None


def sanitized(message: str, key: str) -> str:
    return (message.replace(key, "") if key else message)[: limits.TRANSCRIPTION_ERROR_MAX_CHARS]


async def transcribe(connection: dict, key: str, audio: bytes, mime: str) -> str:
    base = checked_address(connection["base_url"])
    headers = {"Accept-Encoding": "identity"}
    if connection["kind"] == "gemini":
        headers["x-goog-api-key"] = key
        url = f"{base}/models/{connection['model']}:generateContent"
        kwargs = {
            "json": {
                "contents": [
                    {
                        "parts": [
                            {
                                "inlineData": {
                                    "mimeType": mime.partition(";")[0],
                                    "data": base64.b64encode(audio).decode(),
                                }
                            }
                        ]
                    }
                ]
            }
        }
    else:
        if key:
            headers["Authorization"] = f"Bearer {key}"
        url = f"{base}/audio/transcriptions"
        kwargs = {
            "data": {"model": connection["model"]},
            "files": {"file": (f"audio.{FORMATS[mime]}", audio, mime)},
        }
    try:
        async with asyncio.timeout(limits.TRANSCRIPTION_OUTBOUND_SECONDS):
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                async with client.stream("POST", url, headers=headers, **kwargs) as response:
                    data = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(data) + len(chunk) > limits.TRANSCRIPTION_RESPONSE_MAX_BYTES:
                            raise ValueError("Service response exceeded the size limit.")
                        data.extend(chunk)
                    if not 200 <= response.status_code < 300:
                        message = f"Service returned HTTP {response.status_code}."
                        with suppress(ValueError, TypeError, AttributeError):
                            service_error = json.loads(data).get("error", {})
                            if isinstance(service_error, dict) and isinstance(
                                service_error.get("message"), str
                            ):
                                message = service_error["message"]
                        raise ConnectionError(
                            "transcription_upstream_failed", 502, sanitized(message, key)
                        )
                    body = json.loads(data)
                    if not isinstance(body, dict):
                        raise ValueError("Invalid service response.")
                    if connection["kind"] == "gemini":
                        text = "".join(
                            part.get("text", "")
                            for candidate in body.get("candidates", [])
                            for part in candidate.get("content", {}).get("parts", [])
                        )
                    else:
                        text = body.get("text", "")
                    if not isinstance(text, str):
                        raise ValueError("Invalid service response.")
                    return text.replace(key, "") if key else text
    except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError, AttributeError):
        # Transport exceptions may contain URLs and headers; report no raw exception text.
        raise ConnectionError(
            "transcription_upstream_failed",
            502,
            sanitized("The transcription service request failed.", key),
        ) from None


async def check_connection(request: ConnectRequest) -> dict:
    connection = request.configuration()
    formats = []
    failure = "No bundled audio check clips are available."
    for mime, suffix in FORMATS.items():
        clip = Path(__file__).with_name("transcription_clips") / f"check.{suffix}"
        if not clip.is_file():
            continue
        try:
            await transcribe(connection, request.key.get_secret_value(), clip.read_bytes(), mime)
        except ConnectionError as exc:
            failure = exc.message
            continue
        formats.append(mime)
    if not formats:
        raise ConnectionError(
            "connection_check_failed", 422, sanitized(failure, request.key.get_secret_value())
        )
    return {**connection, "formats": formats, "verified_at": datetime.now(UTC).isoformat()}
