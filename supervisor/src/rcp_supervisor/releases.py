"""Resolve and verify complete, promoted public GitHub release bundles.

HTTPS authenticates the GitHub origin; the manifest detects inconsistent assets.
The manifest is not a signature and does not provide independent provenance.
"""

from __future__ import annotations

import fcntl
import hashlib
import http.client
import io
import json
import os
import re
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from rcp_supervisor import limits
from rcp_supervisor.errors import SupervisorError

_REPOSITORY = "Zhi0467/RCP"
_API = f"https://api.github.com/repos/{_REPOSITORY}/releases"
_TAG = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z", re.ASCII)
_VERSION = re.compile(
    r"(?P<base>(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))"
    r"\+build\.(?P<build>[1-9]\d*)\.g(?P<commit>[0-9a-f]{7})\Z",
    re.ASCII,
)
_SUPERVISOR_VERSION = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z", re.ASCII)
_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+\-]*\Z", re.ASCII)
_CHECKSUM = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_FIXED_FILES = {"manifest.sha256", "requirements.lock.txt", "supervisor-requirements.lock.txt"}
# Exact public asset hosts, not a wildcard for arbitrary githubusercontent content:
# https://docs.github.com/en/actions/reference/runners/self-hosted-runners
# https://docs.github.com/en/code-security/reference/supply-chain-security/automatic-dependency-submission
_ASSET_HOSTS = {
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
}


@dataclass(frozen=True)
class VerifiedRelease:
    directory: Path
    manifest_sha256: str
    version: str
    build: int
    commit: str
    wheel: Path
    requirements: Path
    supervisor_wheel: Path
    supervisor_requirements: Path
    supervisor_version: str


def _validate_url(url: str, *, metadata: bool = False) -> None:
    try:
        parsed = urllib.parse.urlsplit(url)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname in ({"api.github.com"} if metadata else _ASSET_HOSTS)
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and not any(character.isspace() or ord(character) < 32 for character in url)
        )
    except ValueError as exc:
        raise SupervisorError("invalid GitHub release URL") from exc
    if not valid:
        raise SupervisorError("release download requires an approved public GitHub HTTPS host")
    if parsed.hostname == "api.github.com" and not parsed.path.startswith(
        f"/repos/{_REPOSITORY}/releases/"
    ):
        raise SupervisorError("release metadata URL is outside the fixed repository")
    if parsed.hostname == "github.com" and not parsed.path.startswith(
        f"/{_REPOSITORY}/releases/download/"
    ):
        raise SupervisorError("release asset URL is outside the fixed repository")


class _GitHubRedirects(urllib.request.HTTPRedirectHandler):
    max_redirections = limits.MAX_REDIRECTS
    max_repeats = limits.MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Metadata remains on the API; asset requests may reach the documented CDN.
        _validate_url(
            newurl, metadata=urllib.parse.urlsplit(req.full_url).hostname == "api.github.com"
        )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(request: urllib.request.Request, timeout: float):
    """The sole transport seam; tests supply an opener to their loopback fixture."""
    return urllib.request.build_opener(_GitHubRedirects()).open(request, timeout=timeout)


def _download(
    url: str,
    output: BinaryIO,
    maximum: int,
    deadline: float,
    *,
    metadata: bool = False,
    expected_size: int | None = None,
) -> None:
    _validate_url(url, metadata=metadata)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SupervisorError("release fetch exceeded its time limit")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json" if metadata else "application/octet-stream",
            "Accept-Encoding": "identity",
            "User-Agent": "rcp-supervisor",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with _open(request, timeout=min(limits.HTTP_TIMEOUT_SECONDS, remaining)) as response:
            _validate_url(response.geturl(), metadata=metadata)
            if response.status != 200:
                raise SupervisorError(f"GitHub release download returned HTTP {response.status}")
            if response.headers.get("Content-Encoding", "identity") != "identity":
                raise SupervisorError("GitHub release download used unexpected content encoding")
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isascii() or not length.isdecimal()):
                raise SupervisorError("GitHub release download has invalid Content-Length")
            if length is not None and int(length) > maximum:
                raise SupervisorError("GitHub release download exceeds its size limit")
            if expected_size is not None and length is not None and int(length) != expected_size:
                raise SupervisorError("GitHub release download size differs from release metadata")
            total = 0
            while True:
                if time.monotonic() >= deadline:
                    raise SupervisorError("release fetch exceeded its time limit")
                # read1 returns after one socket read, so a slow trickle cannot keep
                # filling a large read forever without another deadline check.
                chunk = response.read1(min(limits.DOWNLOAD_CHUNK_BYTES, maximum - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    raise SupervisorError("GitHub release download exceeds its size limit")
                output.write(chunk)
            if (length is not None and total != int(length)) or (
                expected_size is not None and total != expected_size
            ):
                raise SupervisorError("GitHub release download is incomplete")
    except urllib.error.HTTPError as exc:
        raise SupervisorError(
            f"could not download public GitHub release (HTTP {exc.code})"
        ) from exc
    except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
        raise SupervisorError("could not download public GitHub release") from exc


def _bundle_names(names: set[str]) -> tuple[str, str]:
    if any(not _FILENAME.fullmatch(name) for name in names):
        raise SupervisorError("release contains an unsafe asset filename")
    rcp = [name for name in names if name.startswith("rcp-") and name.endswith(".whl")]
    supervisor = [
        name for name in names if name.startswith("rcp_supervisor-") and name.endswith(".whl")
    ]
    if not supervisor or "supervisor-requirements.lock.txt" not in names:
        raise SupervisorError(
            "release is missing the supervisor wheel or supervisor-requirements.lock.txt"
        )
    if len(rcp) != 1 or len(supervisor) != 1:
        raise SupervisorError("release must contain exactly one RCP wheel and one supervisor wheel")
    if names != _FIXED_FILES | {rcp[0], supervisor[0]}:
        raise SupervisorError("release must contain exactly the five required bundle assets")
    return rcp[0], supervisor[0]


def _maximum(name: str) -> int:
    if name == "manifest.sha256":
        return limits.MAX_MANIFEST_BYTES
    return limits.MAX_WHEEL_BYTES if name.endswith(".whl") else limits.MAX_LOCK_BYTES


def _read_regular(path: Path, maximum: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise SupervisorError(f"asset {path.name} exceeds its size limit")
    return data


def _manifest(data: bytes) -> dict[str, str]:
    try:
        lines = data.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise SupervisorError("release manifest is not ASCII") from exc
    expected = {}
    for line in lines:
        digest, separator, name = line.partition("  ")
        if not separator or not _CHECKSUM.fullmatch(digest) or not _FILENAME.fullmatch(name):
            raise SupervisorError("release manifest contains an invalid entry")
        if name in expected:
            raise SupervisorError(f"release manifest contains duplicate asset {name}")
        expected[name] = digest
    _bundle_names(set(expected) | {"manifest.sha256"})
    if "manifest.sha256" in expected:
        raise SupervisorError("release manifest cannot list itself")
    return expected


def _wheel_version(path: Path, distribution: str, data: bytes) -> str:
    parts = path.name.removesuffix(".whl").split("-")
    if len(parts) != 5 or parts[0] != distribution or parts[2:] != ["py3", "none", "any"]:
        raise SupervisorError(f"invalid universal wheel filename: {path.name}")
    version = parts[1]
    expected_metadata = f"{distribution}-{version}.dist-info/METADATA"
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as wheel:
            members = wheel.infolist()
            if len(members) > limits.MAX_WHEEL_MEMBERS:
                raise SupervisorError(f"wheel {path.name} has too many entries")
            if sum(entry.file_size for entry in members) > limits.MAX_UNPACKED_WHEEL_BYTES:
                raise SupervisorError(f"wheel {path.name} unpacked contents exceed the size limit")
            names = set()
            for entry in members:
                name = entry.filename
                if (
                    name in names
                    or "\\" in name
                    or "\x00" in name
                    or PurePosixPath(name).is_absolute()
                    or ".." in PurePosixPath(name).parts
                    or PurePosixPath(name).as_posix() != name.rstrip("/")
                    or stat.S_ISLNK(entry.external_attr >> 16)
                ):
                    raise SupervisorError(
                        f"wheel {path.name} contains an unsafe or duplicate entry"
                    )
                names.add(name)
            metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
            if metadata_names != [expected_metadata]:
                raise SupervisorError(f"wheel {path.name} has inconsistent metadata identity")
            entry = wheel.getinfo(expected_metadata)
            if entry.file_size > limits.MAX_WHEEL_METADATA_BYTES:
                raise SupervisorError(f"wheel {path.name} metadata exceeds its size limit")
            with wheel.open(entry) as source:
                raw = source.read(limits.MAX_WHEEL_METADATA_BYTES + 1)
            if len(raw) > limits.MAX_WHEEL_METADATA_BYTES:
                raise SupervisorError(f"wheel {path.name} metadata exceeds its size limit")
            metadata = BytesParser().parsebytes(raw, headersonly=True)
            names = metadata.get_all("Name", [])
            versions = metadata.get_all("Version", [])
            if (
                len(names) != 1
                or re.sub(r"[-_.]+", "_", names[0]).lower() != distribution
                or versions != [version]
            ):
                raise SupervisorError(f"wheel {path.name} metadata does not match its filename")
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error) as exc:
        raise SupervisorError(f"could not read wheel {path.name}: {exc}") from exc
    return version


def verify_release(directory: Path) -> VerifiedRelease:
    """Verify a complete local bundle without network access or importing its code."""
    try:
        if directory.is_symlink() or not directory.is_dir():
            raise SupervisorError("release bundle must be a real directory")
        entries = list(directory.iterdir())
        rcp, supervisor = _bundle_names({entry.name for entry in entries})
        for entry in entries:
            info = entry.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise SupervisorError(f"release asset {entry.name} must be a regular file")
            if info.st_size > _maximum(entry.name):
                raise SupervisorError(f"asset {entry.name} exceeds its size limit")
        manifest = _read_regular(directory / "manifest.sha256", limits.MAX_MANIFEST_BYTES)
        expected = _manifest(manifest)
        versions = {}
        for name, digest in expected.items():
            # Hash and metadata must describe the same bytes even if the input
            # directory changes. Retain only one bounded asset buffer at a time.
            data = _read_regular(directory / name, _maximum(name))
            if hashlib.sha256(data).hexdigest() != digest:
                raise SupervisorError(f"asset {name} has a SHA-256 mismatch")
            if name in (rcp, supervisor):
                distribution = "rcp" if name == rcp else "rcp_supervisor"
                versions[name] = _wheel_version(directory / name, distribution, data)
            del data
        version = versions[rcp]
        identity = _VERSION.fullmatch(version)
        if identity is None:
            raise SupervisorError("RCP wheel must have a stamped stable build version")
        supervisor_version = versions[supervisor]
        if not _SUPERVISOR_VERSION.fullmatch(supervisor_version):
            raise SupervisorError("supervisor wheel must have a stable X.Y.Z version")
        return VerifiedRelease(
            directory=directory,
            manifest_sha256=hashlib.sha256(manifest).hexdigest(),
            version=version,
            build=int(identity["build"]),
            commit=identity["commit"],
            wheel=directory / rcp,
            requirements=directory / "requirements.lock.txt",
            supervisor_wheel=directory / supervisor,
            supervisor_requirements=directory / "supervisor-requirements.lock.txt",
            supervisor_version=supervisor_version,
        )
    except OSError as exc:
        raise SupervisorError(f"could not verify release bundle: {exc}") from exc


def _release_metadata(selector: str, deadline: float) -> tuple[str, dict[str, dict]]:
    if selector != "stable" and not _TAG.fullmatch(selector):
        raise SupervisorError(
            "release selector must be stable or an exact vX.Y.Z; builds are not deployable"
        )
    endpoint = f"{_API}/latest" if selector == "stable" else f"{_API}/tags/{selector}"
    output = io.BytesIO()
    _download(endpoint, output, limits.MAX_RELEASE_METADATA_BYTES, deadline, metadata=True)
    try:
        release = json.loads(output.getvalue())
    except (ValueError, UnicodeDecodeError) as exc:
        raise SupervisorError("GitHub release metadata is not valid JSON") from exc
    if not isinstance(release, dict):
        raise SupervisorError("GitHub release metadata must be an object")
    tag = release.get("tag_name")
    if (
        release.get("draft") is not False
        or release.get("prerelease") is not False
        or not isinstance(tag, str)
        or not _TAG.fullmatch(tag)
    ):
        raise SupervisorError("GitHub release is not a published stable vX.Y.Z release")
    if selector != "stable" and tag != selector:
        raise SupervisorError("GitHub returned a different release than requested")
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise SupervisorError("GitHub release metadata has no asset list")
    indexed = {}
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            raise SupervisorError("GitHub release asset metadata is invalid")
        name = asset["name"]
        if name in indexed:
            raise SupervisorError(f"GitHub release contains duplicate asset {name}")
        indexed[name] = asset
    _bundle_names(set(indexed))
    for name, asset in indexed.items():
        size = asset.get("size")
        if (
            asset.get("state") != "uploaded"
            or type(size) is not int
            or not 0 <= size <= _maximum(name)
        ):
            raise SupervisorError(f"GitHub release asset {name} has invalid state or size")
        url = asset.get("browser_download_url")
        if not isinstance(url, str):
            raise SupervisorError(f"GitHub release asset {name} has no download URL")
        _validate_url(url)
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.hostname != "github.com"
            or urllib.parse.unquote(parsed.path) != f"/{_REPOSITORY}/releases/download/{tag}/{name}"
            or parsed.query
        ):
            raise SupervisorError(f"GitHub release asset {name} URL does not match the release")
    return tag, indexed


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fetch_release(selector: str, destination: Path) -> VerifiedRelease:
    """Fetch an exact five-file bundle, atomically publishing it at destination.

    The caller owns the cache parent and its permissions. Existing verified
    bundles are immutable: re-fetch checks the remote manifest and never repairs
    or replaces an existing directory. Offline recovery uses verify_release.
    """
    deadline = time.monotonic() + limits.FETCH_TIMEOUT_SECONDS
    tag, assets = _release_metadata(selector, deadline)
    manifest = io.BytesIO()
    _download(
        assets["manifest.sha256"]["browser_download_url"],
        manifest,
        limits.MAX_MANIFEST_BYTES,
        deadline,
        expected_size=assets["manifest.sha256"]["size"],
    )
    expected = _manifest(manifest.getvalue())
    if set(expected) | {"manifest.sha256"} != set(assets):
        raise SupervisorError("release manifest assets differ from GitHub release metadata")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        lock_path = destination.parent / f".{destination.name}.fetch.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a+b") as lock:
            # Bound waiting too: another fetch never turns this into an unattended wait.
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SupervisorError("another fetch owns this release destination") from exc
            if destination.exists() or destination.is_symlink():
                verified = verify_release(destination)
                if (
                    verified.version.split("+", 1)[0] != tag[1:]
                    or verified.manifest_sha256 != hashlib.sha256(manifest.getvalue()).hexdigest()
                ):
                    raise SupervisorError(
                        "existing immutable release bundle differs from the selected release"
                    )
                return verified
            with tempfile.TemporaryDirectory(
                prefix=f".{destination.name}.fetch-", dir=destination.parent
            ) as staging:
                directory = Path(staging)
                for name, asset in assets.items():
                    with (directory / name).open("xb") as output:
                        if name == "manifest.sha256":
                            output.write(manifest.getvalue())
                        else:
                            _download(
                                asset["browser_download_url"],
                                output,
                                _maximum(name),
                                deadline,
                                expected_size=asset["size"],
                            )
                        output.flush()
                        os.fsync(output.fileno())
                verified = verify_release(directory)
                if verified.version.split("+", 1)[0] != tag[1:]:
                    raise SupervisorError("release tag does not match the RCP wheel base version")
                _fsync_directory(directory)
                directory.rename(destination)
                _fsync_directory(destination.parent)
            return verify_release(destination)
    except OSError as exc:
        raise SupervisorError(f"could not publish release bundle: {exc}") from exc
