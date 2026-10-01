"""One bounded public release lookup and its process-local, thread-safe cache."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import threading
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict

from rcp import limits
from rcp.source_checkout import source_checkout_root

REPOSITORY = "Zhi0467/RCP"
API_BASE = f"https://api.github.com/repos/{REPOSITORY}/releases"
_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", re.ASCII)
_COMMIT = re.compile(r"[0-9a-f]{40}", re.ASCII)
_ASSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", re.ASCII)
_GITHUB_HOSTS = {
    "api.github.com",
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
}

ReleaseStatus = Literal[
    "update_available", "current", "pinned", "unchecked", "failed", "off", "unknown"
]


class UpdateNotice(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    space: Literal["team", "personal"]
    status: ReleaseStatus
    current_version: str | None
    latest_version: str | None = None
    checked_at: datetime | None = None
    last_success_at: datetime | None = None
    companion_ready: bool = False
    download_url: str | None = None
    source_checkout: bool = False
    #: The source checkout sits exactly on the latest release commit. A checkout
    #: past it keeps the same base version, so only this can rule out a downgrade.
    source_at_release: bool = False
    update_command: str | None = None


def base_version(version: str | None) -> str | None:
    if version is None:
        return None
    base = version.partition("+")[0]
    return base if _VERSION.fullmatch(base) else None


def _numbers(version: str) -> tuple[int, ...]:
    return tuple(map(int, version.split(".")))


def _github_redirect(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname in _GITHUB_HOSTS
        and parsed.port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


async def _metadata(client: httpx.AsyncClient, url: str) -> dict:
    for _ in range(limits.RELEASE_CHECK_MAX_REDIRECTS + 1):
        async with client.stream("GET", url) as response:
            if response.is_redirect:
                target = str(response.url.join(response.headers["location"]))
                if not _github_redirect(target):
                    raise ValueError("release redirect is outside GitHub")
                url = target
                continue
            response.raise_for_status()
            if response.headers.get("content-encoding", "identity") != "identity":
                raise ValueError("unexpected release content encoding")
            body = bytearray()
            async for chunk in response.aiter_raw():
                body.extend(chunk)
                if len(body) > limits.RELEASE_CHECK_MAX_BYTES:
                    raise ValueError("release metadata exceeds its size limit")
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError("release metadata is not an object")
            return data
    raise ValueError("too many release redirects")


class _UnknownVersion(ValueError):
    pass


def _stable(data: dict) -> tuple[str, str]:
    tag, commit = data.get("tag_name"), data.get("target_commitish")
    if not isinstance(tag, str) or not tag.startswith("v") or not _VERSION.fullmatch(tag[1:]):
        raise _UnknownVersion("release version is malformed")
    if (
        data.get("draft") is not False
        or data.get("prerelease") is not False
        or not isinstance(commit, str)
        or not _COMMIT.fullmatch(commit)
    ):
        raise ValueError("release is not an exact published stable identity")
    return tag[1:], commit


def _checkout_head() -> str | None:
    root = source_checkout_root()
    if root is None:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=limits.RELEASE_CHECK_DEADLINE_SECONDS,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def _companion_ready(data: dict, version: str, commit: str) -> bool:
    if (
        data.get("draft") is not False
        or data.get("tag_name") != f"desktop-v{version}"
        or data.get("target_commitish") != commit
    ):
        return False
    assets = data.get("assets")
    if not isinstance(assets, list):
        return False
    uploaded = {
        asset["name"]
        for asset in assets
        if isinstance(asset, dict)
        and isinstance(asset.get("name"), str)
        and _ASSET_NAME.fullmatch(asset["name"])
        and asset.get("state") == "uploaded"
    }
    return any(name.endswith(".zip") and f"{name}.sha256" in uploaded for name in uploaded)


class ReleaseCheck:
    def __init__(
        self,
        space: Literal["team", "personal"],
        current_version: str | None,
        *,
        pinned: bool = False,
        source_checkout: bool = False,
    ) -> None:
        self._lock = threading.Lock()
        self._check_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pinned = pinned
        self._confirmed: tuple[str, str] | None = None
        self._notice = UpdateNotice(
            space=space,
            status="unchecked",
            current_version=base_version(current_version),
            source_checkout=source_checkout,
        )

    def snapshot(self) -> UpdateNotice:
        with self._lock:
            notice = self._notice
        status = notice.status
        if os.environ.get("RCP_UPDATE_CHECK") == "off":
            status = "off"
        elif self._pinned:
            status = "pinned"
        elif notice.current_version is None:
            status = "unknown"
        command = None
        if status == "update_available":
            if notice.space == "team":
                command = "sudo rcp server update"
            elif notice.source_checkout:
                command = f"scripts/update-from-source v{notice.latest_version}"
        return notice.model_copy(update={"status": status, "update_command": command})

    def check(self, *, companion: bool = True) -> UpdateNotice:
        """One bounded cycle; called by the poller or the explicit CLI doctor."""
        with self._check_lock:
            return self._check(companion)

    def fresh(self, max_age_seconds: float) -> UpdateNotice:
        """The cache, first looked up again when its last check is older than the bound.

        Routes call this so opening a space never shows a result hours old. A
        reader that finds a lookup already running takes the cache rather than
        holding a request worker for it; one lookup serves them all.
        """
        if self._is_fresh(max_age_seconds) or not self._check_lock.acquire(blocking=False):
            return self.snapshot()
        try:
            if self._is_fresh(max_age_seconds):
                return self.snapshot()
            return self._check(companion=True)
        finally:
            self._check_lock.release()

    def _is_fresh(self, max_age_seconds: float) -> bool:
        with self._lock:
            checked = self._notice.checked_at
        return checked is not None and (
            (datetime.now(UTC) - checked).total_seconds() < max_age_seconds
        )

    def _check(self, companion: bool) -> UpdateNotice:
        if os.environ.get("RCP_UPDATE_CHECK") == "off":
            return self.snapshot()
        checked = datetime.now(UTC)
        try:
            version, commit, ready = asyncio.run(self._lookup(companion))
        except (httpx.HTTPError, OSError, ValueError, TimeoutError, KeyError) as exc:
            with self._lock:
                self._notice = self._notice.model_copy(
                    update={
                        "status": "unknown" if isinstance(exc, _UnknownVersion) else "failed",
                        "checked_at": checked,
                    }
                )
        else:
            at_release = self._notice.source_checkout and _checkout_head() == commit
            with self._lock:
                current = self._notice.current_version
                newer = current is not None and _numbers(version) > _numbers(current)
                self._notice = self._notice.model_copy(
                    update={
                        "status": "update_available" if newer else "current",
                        "latest_version": version,
                        "checked_at": checked,
                        "last_success_at": datetime.now(UTC),
                        "companion_ready": ready,
                        "source_at_release": at_release,
                        "download_url": f"https://github.com/{REPOSITORY}/releases/tag/desktop-v{version}"
                        if ready
                        else None,
                    }
                )
        return self.snapshot()

    async def _lookup(self, companion: bool) -> tuple[str, str, bool]:
        async with asyncio.timeout(limits.RELEASE_CHECK_DEADLINE_SECONDS):
            async with httpx.AsyncClient(
                timeout=limits.RELEASE_CHECK_DEADLINE_SECONDS,
                follow_redirects=False,
                trust_env=False,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Accept-Encoding": "identity",
                    "User-Agent": "rcp-release-check",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            ) as client:
                version, commit = _stable(await _metadata(client, f"{API_BASE}/latest"))
                ready = self._confirmed == (version, commit)
                current = self.snapshot().current_version
                if (
                    companion
                    and not ready
                    and current is not None
                    and _numbers(version) > _numbers(current)
                ):
                    try:
                        data = await _metadata(client, f"{API_BASE}/tags/desktop-v{version}")
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code != 404:
                            raise
                    else:
                        ready = _companion_ready(data, version, commit)
                        if ready:
                            self._confirmed = (version, commit)
                return version, commit, ready

    def start(self) -> None:
        if self._thread is not None or os.environ.get("RCP_UPDATE_CHECK") == "off":
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll, name="rcp-release-check", daemon=True)
        self._thread.start()

    def _poll(self) -> None:
        if self._stop.wait(limits.RELEASE_CHECK_START_DELAY_SECONDS):
            return
        while not self._stop.is_set():
            self.check()
            if self._stop.wait(limits.RELEASE_CHECK_INTERVAL_SECONDS):
                return

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
