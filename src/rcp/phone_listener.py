"""The personal space's notify-only phone listener.

A personal backend treats every request as the owner, so a phone never reaches
the owner API. This separate listener serves only the pairing page, the web-app
manifest, the service worker, its icons, and the registration route, and it
runs only while a pairing code is live. The person routes one HTTPS name to it,
for example with ``tailscale serve``.
"""

from __future__ import annotations

import threading
import time

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from rcp import web_push
from rcp.limits import (
    NOTIFICATION_STOP_TIMEOUT_SECONDS,
    PHONE_LISTENER_POLL_SECONDS,
    TEAM_DEVICE_PAIRING_CODE_MAX_LENGTH,
    WEB_PUSH_MAX_ENDPOINT_BYTES,
)
from rcp.storage import AppStore
from rcp.web_assets import web_dist_path

PHONE_LISTENER_HOST = "127.0.0.1"
PHONE_LISTENER_PORT = 8422
# Files the phone needs from the built web app; nothing else is served.
_PAIRING_PAGE = "phone.html"
_WEB_APP_FILES = {
    "manifest.webmanifest": "application/manifest+json",
    "sw.js": "text/javascript",
    "icon-180.png": "image/png",
    "icon-512.png": "image/png",
}


class PhoneRegistration(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: str = Field(min_length=1, max_length=TEAM_DEVICE_PAIRING_CODE_MAX_LENGTH)
    endpoint: str = Field(min_length=1, max_length=WEB_PUSH_MAX_ENDPOINT_BYTES)
    keys: web_push.WebPushKeys


def create_phone_listener_app(store: AppStore, resolve: web_push.Resolver) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/")
    def pairing_page() -> FileResponse:
        path = web_dist_path() / _PAIRING_PAGE
        if not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="text/html", headers={"Cache-Control": "no-store"})

    @app.get("/{name}")
    def web_app_file(name: str) -> FileResponse:
        media_type = _WEB_APP_FILES.get(name)
        path = web_dist_path() / name
        if media_type is None or not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type=media_type)

    @app.get("/api/key")
    def key() -> dict[str, str]:
        pem = store.notification_vapid_key()
        return {"application_server_key": web_push.VapidKey.from_pem(pem).application_server_key}

    @app.post("/api/register")
    def register(body: PhoneRegistration, request: Request) -> dict[str, bool]:
        subscription = web_push.Subscription(
            endpoint=body.endpoint,
            p256dh=body.keys.p256dh,
            auth=body.keys.auth,
            origin=request.headers.get("origin", ""),
        )
        try:
            web_push.validate_subscription(subscription, resolve=resolve)
        except web_push.WebPushRefused as exc:
            raise HTTPException(status_code=422, detail={"code": "subscription_refused"}) from exc
        store.notification_vapid_key()
        try:
            store.redeem_notification_phone_pairing(
                body.code,
                endpoint=subscription.endpoint,
                p256dh=subscription.p256dh,
                auth=subscription.auth,
                origin=subscription.origin,
            )
        except ValueError as exc:
            raise HTTPException(status_code=403, detail={"code": f"code_{exc}"}) from exc
        return {"ok": True}

    return app


class PhoneListener:
    """Serve the listener on loopback while a pairing code is live."""

    def __init__(
        self,
        store: AppStore,
        resolve: web_push.Resolver,
        *,
        host: str = PHONE_LISTENER_HOST,
        port: int = PHONE_LISTENER_PORT,
    ) -> None:
        self.store = store
        self.app = create_phone_listener_app(store, resolve)
        self.host = host
        self.port = port
        self._lock = threading.Lock()
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def ensure_running(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            config = uvicorn.Config(
                self.app, host=self.host, port=self.port, log_level="warning", lifespan="off"
            )
            self._server = uvicorn.Server(config)
            self._thread = threading.Thread(
                target=self._serve, args=(self._server,), name="rcp-phone-listener", daemon=True
            )
            self._thread.start()

    def _serve(self, server: uvicorn.Server) -> None:
        watchdog = threading.Thread(target=self._stop_when_no_code, args=(server,), daemon=True)
        watchdog.start()
        server.run()

    def _stop_when_no_code(self, server: uvicorn.Server) -> None:
        while not server.should_exit:
            time.sleep(PHONE_LISTENER_POLL_SECONDS)
            if not self.store.notification_phone_pairing_live():
                server.should_exit = True

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self, timeout: float = NOTIFICATION_STOP_TIMEOUT_SECONDS) -> None:
        with self._lock:
            if self._server is not None:
                self._server.should_exit = True
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
