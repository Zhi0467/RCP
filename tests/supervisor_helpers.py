from __future__ import annotations

import base64
import csv
import hashlib
import io
import zipfile
from pathlib import Path

VERSION = "0.3.2+build.412.gfe06636"
RCP_WHEEL = f"rcp-{VERSION}-py3-none-any.whl"
SUPERVISOR_WHEEL = "rcp_supervisor-0.1.0-py3-none-any.whl"


def wheel_bytes(distribution: str, version: str, *, metadata_version: str | None = None) -> bytes:
    info = f"{distribution}-{version}.dist-info"
    contents = {
        f"{distribution}/__init__.py": f'__version__ = "{version}"\n'.encode(),
        f"{info}/METADATA": (
            f"Metadata-Version: 2.3\nName: {distribution}\nVersion: {metadata_version or version}\n"
        ).encode(),
        f"{info}/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    records = io.StringIO()
    writer = csv.writer(records, lineterminator="\n")
    for name, data in contents.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        writer.writerow([name, f"sha256={digest}", str(len(data))])
    writer.writerow([f"{info}/RECORD", "", ""])
    contents[f"{info}/RECORD"] = records.getvalue().encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as wheel:
        for name, data in contents.items():
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            wheel.writestr(entry, data)
    return output.getvalue()


def bundle_assets() -> dict[str, bytes]:
    assets = {
        RCP_WHEEL: wheel_bytes("rcp", VERSION),
        SUPERVISOR_WHEEL: wheel_bytes("rcp_supervisor", "0.1.0"),
        "requirements.lock.txt": b"# uv export of pinned and hashed dependencies\n",
        "supervisor-requirements.lock.txt": b"# No runtime dependencies\n",
    }
    refresh_manifest(assets)
    return assets


def refresh_manifest(assets: dict[str, bytes]) -> None:
    assets["manifest.sha256"] = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n"
        for name, data in sorted(assets.items())
        if name != "manifest.sha256"
    ).encode()


def make_bundle(directory: Path, assets: dict[str, bytes] | None = None) -> Path:
    directory.mkdir()
    for name, data in (assets or bundle_assets()).items():
        (directory / name).write_bytes(data)
    return directory
