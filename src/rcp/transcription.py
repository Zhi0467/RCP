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
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from rcp import limits
from rcp.service_connections import (
    ConnectionError,
    ModelId,
    PurposesRequest,
    connection_purposes,
)

FORMATS = {"audio/webm;codecs=opus": "webm", "audio/mp4;codecs=mp4a.40.2": "mp4"}
PRESETS = {
    "openai": ("https://api.openai.com/v1", "gpt-transcribe", "OpenAI"),
    "groq": ("https://api.groq.com/openai/v1", "whisper-large-v3-turbo", "Groq"),
}
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_MODEL = "gemini-3.5-transcribe"


class ServiceAddress(BaseModel):
    """Where a service lives and the key it takes, before anything is saved."""

    model_config = ConfigDict(extra="forbid", strict=True)
    kind: str
    preset: str | None = None
    base_url: str | None = None
    key: SecretStr = Field(default=SecretStr(""), max_length=limits.TRANSCRIPTION_KEY_MAX_CHARS)

    def address(self) -> dict:
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
        # A secret must not be copied into public metadata or an HTTP request URL.
        if (not key and self.preset != "custom") or (key and (key in base or key in label)):
            raise ConnectionError("connection_check_failed", 422, "Invalid service configuration.")
        return dict(kind=self.kind, preset=self.preset, base_url=base, model=model, label=label)


class ConnectRequest(PurposesRequest, ServiceAddress):
    purposes: list[Literal["transcription", "voice"]] = Field(
        default_factory=lambda: ["transcription"], min_length=1, max_length=2
    )
    model_config = ConfigDict(extra="forbid", strict=True)
    model: str | None = Field(default=None, max_length=200)
    delegation_model: ModelId | None = None

    def configuration(self) -> dict:
        address = self.address()
        model = self.model or address["model"]
        key = self.key.get_secret_value()
        if not re.fullmatch(r"[A-Za-z0-9._:/-]+", model) or (key and key in model):
            raise ConnectionError("connection_check_failed", 422, "Invalid service configuration.")
        return dict(id=uuid.uuid4().hex, **address, purposes=self.purposes) | {"model": model}


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
        status, data = await service_request("POST", url, headers=headers, **kwargs)
        if not 200 <= status < 300:
            message = f"Service returned HTTP {status}."
            with suppress(ValueError, TypeError, AttributeError):
                service_error = json.loads(data).get("error", {})
                if isinstance(service_error, dict) and isinstance(
                    service_error.get("message"), str
                ):
                    message = service_error["message"]
            raise ConnectionError("transcription_upstream_failed", 502, sanitized(message, key))
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
        # Rewriting the text would corrupt dictation, so a reply carrying the
        # key is discarded instead.
        if key and key in text:
            raise ConnectionError(
                "transcription_upstream_failed",
                502,
                "The service's reply contained the connection's key, so RCP discarded it.",
            )
        return text
    except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError, AttributeError):
        # Transport exceptions may contain URLs and headers; report no raw exception text.
        raise ConnectionError(
            "transcription_upstream_failed",
            502,
            sanitized("The transcription service request failed.", key),
        ) from None


async def service_request(method: str, url: str, **kwargs) -> tuple[int, bytes]:
    """One deadline and bounded raw response for member service calls."""
    async with asyncio.timeout(limits.TRANSCRIPTION_OUTBOUND_SECONDS):
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            async with client.stream(method, url, **kwargs) as response:
                data = bytearray()
                async for chunk in response.aiter_raw():
                    if len(data) + len(chunk) > limits.TRANSCRIPTION_RESPONSE_MAX_BYTES:
                        raise ValueError("Service response exceeded the size limit.")
                    data.extend(chunk)
                return response.status_code, bytes(data)


async def check_connection(request: ConnectRequest) -> dict:
    connection = request.configuration()
    return await check_purposes(
        connection, request.key.get_secret_value(), request.purposes, request.delegation_model
    )


async def check_purposes(
    connection: dict, key: str, added: list[str], delegation_model: str | None = None
) -> dict:
    if key and (key in connection["model"] or key in (delegation_model or "")):
        raise ConnectionError("connection_check_failed", 422, "Invalid service configuration.")
    if "voice" in connection_purposes(connection):
        # Import locally: voice uses the shared bounded transport above.
        from rcp.voice import check_delegation_model, check_voice_connection, require_openai

        require_openai(connection)
        if "voice" in added:
            await check_voice_connection(connection, key)
        if delegation_model is not None:
            await check_delegation_model(connection, key, delegation_model)
    result = {"formats": [], **connection}
    if "transcription" in added:
        result["formats"] = await check_transcription(connection, key)
    if added:
        result["verified_at"] = datetime.now(UTC).isoformat()
    return result


async def check_transcription(connection: dict, key: str) -> list[str]:
    formats = []
    failure = "No bundled audio check clips are available."
    for mime, suffix in FORMATS.items():
        clip = Path(__file__).with_name("transcription_clips") / f"check.{suffix}"
        if not clip.is_file():
            continue
        try:
            await transcribe(connection, key, clip.read_bytes(), mime)
        except ConnectionError as exc:
            failure = exc.message
            continue
        formats.append(mime)
    if not formats:
        raise ConnectionError("connection_check_failed", 422, sanitized(failure, key))
    return formats


# Provider model lists carry ids but no capability flags, so names pick the
# candidates; the connection check still decides whether a chosen id works.
_TRANSCRIPTION_HINTS = ("transcribe", "whisper")
_NOT_DELEGATION = ("transcribe", "tts", "audio", "realtime", "live", "image", "search")


async def list_models(connection: dict, key: str) -> dict[str, list[str]]:
    """The provider's current model ids, split into dictation and delegation candidates."""
    base = checked_address(connection["base_url"])
    headers = {"Accept-Encoding": "identity"}
    gemini = connection["kind"] == "gemini"
    if gemini:
        headers["x-goog-api-key"] = key
        url = f"{base}/models?pageSize=1000"
    else:
        if key:
            headers["Authorization"] = f"Bearer {key}"
        url = f"{base}/models"
    try:
        status, data = await service_request("GET", url, headers=headers)
        if not 200 <= status < 300:
            raise ConnectionError("model_list_failed", 502, f"Service returned HTTP {status}.")
        body = json.loads(data)
        if gemini:
            ids = [
                entry["name"].removeprefix("models/")
                for entry in body["models"]
                if "generateContent" in entry.get("supportedGenerationMethods", [])
            ]
        else:
            ids = [entry["id"] for entry in body["data"] if not entry.get("shutdown_date")]
    except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError, AttributeError):
        raise ConnectionError(
            "model_list_failed", 502, "The service did not return a model list."
        ) from None
    ids = sorted(
        {
            item
            for item in ids
            if isinstance(item, str)
            and len(item) <= 200
            and re.fullmatch(r"[A-Za-z0-9._:/-]+", item)
            and not (key and key in item)
        }
    )
    named = [i for i in ids if any(h in i for h in _TRANSCRIPTION_HINTS) and "diarize" not in i]
    # Gemini and custom servers transcribe through general models too: named ones first.
    open_list = gemini or connection["preset"] == "custom"
    delegation = []
    if connection["preset"] == "openai":
        delegation = [
            i for i in ids if re.match(r"gpt-\d", i) and not any(w in i for w in _NOT_DELEGATION)
        ]
    return {
        "transcription": named + [i for i in ids if i not in named] if open_list else named,
        "delegation": delegation,
    }
