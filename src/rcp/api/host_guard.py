"""Validate request authorities before backend admission, including public health."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_DESKTOP_RELAY_HOST = re.compile(r"rcp-[0-9a-f]{32}\.rcp\.localhost:([0-9]{1,5})")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})


class HostGuard:
    """Accept loopback, the desktop relay, and the operator's team origin.

    Relay ports belong to the HTTPS front end, independently of the backend's
    listener. Forwarded headers never grant an additional accepted authority.
    """

    def __init__(self, app: ASGIApp, *, port: int, team_access_url: str | None = None) -> None:
        self.app = app
        self.port = port
        self.team_authorities: frozenset[str] = frozenset()
        if team_access_url is not None:
            origin = urlsplit(team_access_url)
            authority = origin.netloc.lower()
            authorities = {authority}
            if origin.scheme == "https" and origin.port in {None, 443}:
                authorities.add(f"{origin.hostname}:443")
                authorities.add(str(origin.hostname))
            self.team_authorities = frozenset(authorities)

    def _accepts(self, host: str, scheme: str) -> bool:
        host = host.lower()
        if host in self.team_authorities:
            return True
        if any(host == f"{name}:{self.port}" for name in _LOOPBACK_HOSTS):
            return True
        default_port = 443 if scheme in {"https", "wss"} else 80
        if self.port == default_port and host in _LOOPBACK_HOSTS:
            return True
        relay = _DESKTOP_RELAY_HOST.fullmatch(host)
        return relay is not None and 0 < int(relay.group(1)) <= 65_535

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        hosts = [value for name, value in scope["headers"] if name.lower() == b"host"]
        if len(hosts) == 1 and self._accepts(hosts[0].decode("latin-1"), scope["scheme"]):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        response = JSONResponse(status_code=421, content={"detail": {"code": "host_invalid"}})
        await response(scope, receive, send)
