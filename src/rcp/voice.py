"""Stateless GPT-Live session exchange using private member service credentials."""

from __future__ import annotations

import json
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from rcp import limits
from rcp.service_connections import ConnectionError
from rcp.transcription import PRESETS, service_request

MODEL = "gpt-live-1"
INSTRUCTIONS = (
    "You are RCP's voice assistant, acting as the authenticated member. "
    "Act only through the supplied tools; delegate actions to the tool backend. "
    "Never claim an action ran until its tool result confirms it. "
    "Never list capabilities."
)


class FunctionTool(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["function"]
    name: str = Field(min_length=1)
    description: str
    parameters: dict[str, Any]


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sdp_offer: str = Field(min_length=1, max_length=limits.VOICE_SDP_MAX_CHARS)
    tools: list[FunctionTool]

    @field_validator("tools", mode="before")
    @classmethod
    def bounded_tools(cls, value):
        if len(json.dumps(value, ensure_ascii=False).encode()) > limits.VOICE_TOOLS_MAX_BYTES:
            raise ValueError("Tools exceed the size limit")
        return value


def require_openai(connection: dict) -> None:
    if (
        connection["kind"] != "openai_compatible"
        or connection["preset"] != "openai"
        or connection["base_url"] != PRESETS["openai"][0]
    ):
        raise ConnectionError("voice_connection_unsupported", 422)


async def _request(connection: dict, key: str, method: str, path: str, **kwargs) -> dict:
    require_openai(connection)
    try:
        status, data = await service_request(
            method,
            f"{PRESETS['openai'][0]}/{path}",
            headers={"Authorization": f"Bearer {key}", "Accept-Encoding": "identity"},
            **kwargs,
        )
        if not 200 <= status < 300:
            raise ValueError
        body = json.loads(data)
        if not isinstance(body, dict):
            raise ValueError
        return body
    except (httpx.HTTPError, TimeoutError, ValueError, TypeError):
        # Even fragments of a key can appear in upstream errors. Never relay them.
        raise ConnectionError(
            "voice_upstream_failed", 502, "The voice service request failed."
        ) from None


async def check_voice_connection(connection: dict, key: str) -> None:
    # An authenticated model lookup checks access without starting a paid session.
    try:
        body = await _request(connection, key, "GET", f"models/{MODEL}")
        if body.get("id") != MODEL:
            raise ConnectionError("voice_upstream_failed", 502)
    except ConnectionError:
        raise ConnectionError(
            "connection_check_failed", 422, "The voice connection check failed."
        ) from None


async def create_session(connection: dict, key: str, model: str, offer: SessionRequest) -> str:
    body = await _request(
        connection,
        key,
        "POST",
        "live/sessions",
        json={
            "session": {
                "model": MODEL,
                # MediaSessionConfig exposes no configurable duration limit.
                # The page enforces VOICE_HARD_CAP_SECONDS and closes on hiding.
                "store": False,
                "instructions": INSTRUCTIONS,
                "delegation": {
                    "type": "responses",
                    "responses": {
                        "model": model,
                        "instructions": INSTRUCTIONS,
                        "parallel_tool_calls": False,
                        "tools": [tool.model_dump() for tool in offer.tools],
                    },
                },
            },
            "transport": {"type": "webrtc", "sdp": offer.sdp_offer},
        },
    )
    transport = body.get("transport")
    answer = transport.get("sdp") if isinstance(transport, dict) else None
    if not isinstance(answer, str) or not answer.strip() or (key and key in answer):
        raise ConnectionError(
            "voice_upstream_failed", 502, "The voice service response was invalid."
        )
    return answer
