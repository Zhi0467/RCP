"""Checksum-pinned, backend-owned preview runtimes; never discover engines on PATH.

The typst.ts 0.7.0 bundle's defaultAssets is ['text']: its 17 fonts come from
typst-assets v0.13.1. Clients must pass these same-origin font URLs explicitly
(and the two WASM URLs), so the upstream defaults never fetch from a CDN.
"""

from __future__ import annotations

import asyncio
import hashlib
import platform
import shutil
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

from rcp.limits import (
    PAPER_COMPILE_DOWNLOAD_TIMEOUT_SECONDS,
    PAPER_COMPILE_MAX_ASSET_BYTES,
)

EngineName = Literal["latex", "typst"]
EngineState = Literal["ready", "installing", "unavailable"]


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    sha256: str


TYPST_ASSETS = (
    Asset(
        "all-in-one-lite.bundle.js",
        "https://cdn.jsdelivr.net/npm/@myriaddreamin/typst.ts@0.7.0/dist/esm/contrib/all-in-one-lite.bundle.js",
        "9999a19a74f27810e8d951849f208a9b556fa36b5c0a726c052f1e5bb8ba9200",
    ),
    Asset(
        "typst_ts_web_compiler_bg.wasm",
        "https://cdn.jsdelivr.net/npm/@myriaddreamin/typst-ts-web-compiler@0.7.0/pkg/typst_ts_web_compiler_bg.wasm",
        "1fc968438a672366dfec39c96c842c26ed29caff4eb1bcaab19a6c60867de5fd",
    ),
    Asset(
        "typst_ts_renderer_bg.wasm",
        "https://cdn.jsdelivr.net/npm/@myriaddreamin/typst-ts-renderer@0.7.0/pkg/typst_ts_renderer_bg.wasm",
        "5a93d7a8b3b7b43b0e679f3dafed966f74fc1653cc7ed6623f04a8a6550da725",
    ),
    Asset(
        "DejaVuSansMono-Bold.ttf",
        "https://cdn.jsdelivr.net/gh/typst/typst-assets@v0.13.1/files/fonts/DejaVuSansMono-Bold.ttf",
        "bce60f1b4421acd9ea51ba6623d7024ecbe6817a953e3654df62a5e6bdf8f769",
    ),
    Asset(
        "DejaVuSansMono-BoldOblique.ttf",
        "https://cdn.jsdelivr.net/gh/typst/typst-assets@v0.13.1/files/fonts/DejaVuSansMono-BoldOblique.ttf",
        "91713a71d550bba22c2a6b2bb2a9ad8f9a159e12e4e9f0a5b2677998ba21213e",
    ),
    Asset(
        "DejaVuSansMono-Oblique.ttf",
        "https://cdn.jsdelivr.net/gh/typst/typst-assets@v0.13.1/files/fonts/DejaVuSansMono-Oblique.ttf",
        "742097840c541870e8d6dc5c9b37bb1ceeea6c0dedd1d475faf903ef9df734b0",
    ),
    Asset(
        "DejaVuSansMono.ttf",
        "https://cdn.jsdelivr.net/gh/typst/typst-assets@v0.13.1/files/fonts/DejaVuSansMono.ttf",
        "b4a6c3e4faab8773f4ff761d56451646409f29abedd68f05d38c2df667d3c582",
    ),
    Asset(
        "LibertinusSerif-Bold.otf",
        "https://cdn.jsdelivr.net/gh/typst/typst-assets@v0.13.1/files/fonts/LibertinusSerif-Bold.otf",
        "0264914210ed51b3231ebc92ce529e9f2e166ba9eebf0cd4a579558690a27b64",
    ),
    Asset(
        "LibertinusSerif-BoldItalic.otf",
        "https://cdn.jsdelivr.net/gh/typst/typst-assets@v0.13.1/files/fonts/LibertinusSerif-BoldItalic.otf",
        "47a665259f09f554f5d133d7718cdad43ff462c6a6b2328f38023465e62d57ce",
    ),
    Asset(
        "LibertinusSerif-Italic.otf",
        "https://cdn.jsdelivr.net/gh/typst/typst-assets@v0.13.1/files/fonts/LibertinusSerif-Italic.otf",
        "9a393d63d6e05f620d3dc0190dfd35a8ede58c0808cf0fc9de7fcb9c723e4c24",
    ),
    Asset(
        "LibertinusSerif-Regular.otf",
        "https://cdn.jsdelivr.net/gh/typst/typst-assets@v0.13.1/files/fonts/LibertinusSerif-Regular.otf",
        "fcf06307a77367394fcb0ccb241e59eea70dba3d732be309647611224679c733",
    ),
    Asset(
        "LibertinusSerif-Semibold.otf",
        "https://cdn.jsdelivr.net/gh/typst/typst-assets@v0.13.1/files/fonts/LibertinusSerif-Semibold.otf",
        "a4b3f28e85881db34695c1f005e4c79233a6caf3a2bd286c9b418c025fb99308",
    ),
    Asset(
        "LibertinusSerif-SemiboldItalic.otf",
        "https://cdn.jsdelivr.net/gh/typst/typst-assets@v0.13.1/files/fonts/LibertinusSerif-SemiboldItalic.otf",
        "397f0d7aba35ae6a988948ae046c14c6b1e0d270fcc6e292b8bc29fd625c6101",
    ),
    Asset(
        "NewCM10-Bold.otf",
        "https://cdn.jsdelivr.net/gh/typst/typst-assets@v0.13.1/files/fonts/NewCM10-Bold.otf",
        "947931c42ca3a87c369b81d6de98533e0d5e4b2d713950781664644fb7d59724",
    ),
    Asset(
        "NewCM10-BoldItalic.otf",
        "https://cdn.jsdelivr.net/gh/typst/typst-assets@v0.13.1/files/fonts/NewCM10-BoldItalic.otf",
        "0ddd9bab5b7d524602a27604f43e3be5f7c0968f7956b73733f7095f2036f1d2",
    ),
    Asset(
        "NewCM10-Italic.otf",
        "https://cdn.jsdelivr.net/gh/typst/typst-assets@v0.13.1/files/fonts/NewCM10-Italic.otf",
        "70c9c811bb0e4bcb36d1f18e364cd770cc5b781f36f127506e2bd46be19376d1",
    ),
    Asset(
        "NewCM10-Regular.otf",
        "https://cdn.jsdelivr.net/gh/typst/typst-assets@v0.13.1/files/fonts/NewCM10-Regular.otf",
        "2f751e3082cee78652f6f07f1614e653d2b395b33a0834b324990e354e31d077",
    ),
    Asset(
        "NewCMMath-Bold.otf",
        "https://cdn.jsdelivr.net/gh/typst/typst-assets@v0.13.1/files/fonts/NewCMMath-Bold.otf",
        "8956f7ef6c212ea647fe689abb66a3f3359126abcbb673fb58f3ecf033c7d1a7",
    ),
    Asset(
        "NewCMMath-Book.otf",
        "https://cdn.jsdelivr.net/gh/typst/typst-assets@v0.13.1/files/fonts/NewCMMath-Book.otf",
        "b2e655d5cae5ab9569fa6f5d94e929156fe70bd3548d916b4b23d1a1c1a55693",
    ),
    Asset(
        "NewCMMath-Regular.otf",
        "https://cdn.jsdelivr.net/gh/typst/typst-assets@v0.13.1/files/fonts/NewCMMath-Regular.otf",
        "bfd2f9b22caacd41b8b29cfe3f6d72f976f221a65362e07eda35cbae863721b7",
    ),
)

TECTONIC_ASSETS = {
    ("Darwin", "arm64"): Asset(
        "tectonic-0.17.0-aarch64-apple-darwin.tar.gz",
        "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%400.17.0/tectonic-0.17.0-aarch64-apple-darwin.tar.gz",
        "a3f1cac7c5678f01661a92212f58480ae3b0634115d880dbc59e2953ded45667",
    ),
    ("Linux", "x86_64"): Asset(
        "tectonic-0.17.0-x86_64-unknown-linux-musl.tar.gz",
        "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%400.17.0/tectonic-0.17.0-x86_64-unknown-linux-musl.tar.gz",
        "8533d07f9ccbd7a65824b9e0459041bca34af1eb33daba48f59215593753a3b7",
    ),
}

TECTONIC_BINARY_HASHES = {
    "tectonic-0.17.0-aarch64-apple-darwin.tar.gz": "b52b5a730e2b0b33087304f7720f649603953f270a6b1c88bb031e1ae01f7f9c",
    "tectonic-0.17.0-x86_64-unknown-linux-musl.tar.gz": "a98aa59ad5c1df39a6c9e56cbfc5088f2b11d6c179c0130b97998e4bd46a46da",
}


def _download(url: str, dest: Path) -> None:
    started = time.monotonic()
    size = 0
    with httpx.stream(
        "GET", url, follow_redirects=True, timeout=PAPER_COMPILE_DOWNLOAD_TIMEOUT_SECONDS
    ) as response:
        response.raise_for_status()
        with dest.open("wb") as stream:
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > PAPER_COMPILE_MAX_ASSET_BYTES:
                    raise ValueError("Engine asset exceeds size limit")
                if time.monotonic() - started > PAPER_COMPILE_DOWNLOAD_TIMEOUT_SECONDS:
                    raise TimeoutError("Engine asset download timed out")
                stream.write(chunk)


def _verify(path: Path, digest: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Missing regular engine asset: {path.name}")
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != digest:
        raise ValueError(f"Engine asset checksum mismatch: {path.name}")


def _stat(path: Path) -> tuple[int, int]:
    try:
        stat = path.lstat()
    except OSError:
        return (-1, -1)
    return (stat.st_size, stat.st_mtime_ns)


class EngineStore:
    """One per backend, owned by its main event loop; installs survive caller cancellation."""

    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "paper-engines"
        self._tasks: dict[str, asyncio.Task[bool]] = {}
        self._errors: dict[str, str] = {}
        # Hashing the Typst set reads ~35 MB: hash once per process, then compare stats.
        self._validated: dict[str, tuple[Path, dict[str, tuple[int, int]]]] = {}

    def _directory(self, name: str) -> Path:
        if name == "typst":
            return self.root / "typst-0.7.0-v1"
        if name == "latex":
            asset = self._tectonic()
            return self.root / asset.name.removesuffix(".tar.gz")
        raise ValueError("Unknown paper engine")

    @staticmethod
    def _tectonic() -> Asset:
        try:
            return TECTONIC_ASSETS[(platform.system(), platform.machine())]
        except KeyError as exc:
            raise ValueError("Pinned Tectonic is unavailable on this platform") from exc

    def _hashes(self, name: str) -> dict[str, str]:
        if name == "typst":
            return {asset.name: asset.sha256 for asset in TYPST_ASSETS}
        return {"tectonic": TECTONIC_BINARY_HASHES[self._tectonic().name]}

    def _validate(self, name: str) -> Path:
        cached = self._validated.get(name)
        if cached is not None:
            dest, stats = cached
            if all(_stat(dest / filename) == stat for filename, stat in stats.items()):
                return dest
            del self._validated[name]
        dest = self._directory(name)
        dest.lstat()  # A missing installation is unavailable, not a failed installation.
        if dest.is_symlink():
            raise ValueError("Engine directory must not be a symlink")
        for filename, digest in self._hashes(name).items():
            _verify(dest / filename, digest)
        self._validated[name] = (
            dest,
            {filename: _stat(dest / filename) for filename in self._hashes(name)},
        )
        return dest

    def status(self, name: str) -> EngineState:
        task = self._tasks.get(name)
        if task is not None and not task.done():
            return "installing"
        try:
            self._validate(name)
        except (OSError, ValueError) as exc:
            # A never-installed engine is not a failure; it is offered for install.
            if not isinstance(exc, FileNotFoundError):
                self._errors.setdefault(name, str(exc))
            return "unavailable"
        self._errors.pop(name, None)
        return "ready"

    def error(self, name: str) -> str:
        return self._errors.get(name, "")

    def start_install(self, name: str) -> None:
        self._directory(name)  # Reject unsupported names/platforms before scheduling.
        if self.status(name) in {"ready", "installing"}:
            return
        self._errors.pop(name, None)
        self._validated.pop(name, None)
        self._tasks[name] = asyncio.create_task(self._install(name))

    async def ensure(self, name: str) -> bool:
        try:
            self.start_install(name)
        except ValueError as exc:
            self._errors[name] = str(exc)
            return False
        task = self._tasks.get(name)
        if task is not None:
            return await asyncio.shield(task)
        return self.status(name) == "ready"

    async def _install(self, name: str) -> bool:
        try:
            await asyncio.to_thread(self._install_sync, name)
        except (OSError, ValueError, httpx.HTTPError, tarfile.TarError) as exc:
            self._errors[name] = str(exc)
            return False
        self._errors.pop(name, None)
        return True

    def _install_sync(self, name: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        dest = self._directory(name)
        with tempfile.TemporaryDirectory(prefix=".install-", dir=self.root) as temp:
            stage = Path(temp) / "ready"
            stage.mkdir()
            assets = TYPST_ASSETS if name == "typst" else (self._tectonic(),)
            for asset in assets:
                target = stage / asset.name
                _download(asset.url, target)
                _verify(target, asset.sha256)
                if name == "latex":
                    with tarfile.open(target) as archive:
                        member = archive.getmember("tectonic")
                        if not member.isfile() or member.size > PAPER_COMPILE_MAX_ASSET_BYTES:
                            raise ValueError("Invalid Tectonic binary in archive")
                        source = archive.extractfile(member)
                        if source is None:
                            raise ValueError("Missing Tectonic binary in archive")
                        with source, (stage / "tectonic").open("wb") as output:
                            shutil.copyfileobj(source, output)
                    _verify(stage / "tectonic", self._hashes(name)["tectonic"])
                    (stage / "tectonic").chmod(0o700)
                    target.unlink()
            # Invalid previous installs are moved aside, never overlaid with partial files.
            if dest.exists() or dest.is_symlink():
                dest.rename(Path(temp) / "previous")
            stage.rename(dest)

    def binary(self) -> Path:
        return self._validate("latex") / "tectonic"

    def asset(self, asset: str) -> Path:
        if asset not in self._hashes("typst"):
            raise ValueError("Unknown Typst asset")
        return self._validate("typst") / asset

    async def close(self) -> None:
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
