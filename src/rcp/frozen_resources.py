"""Build-derived resource inventory for the frozen desktop backend."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

MANIFEST_NAME = "_resource_manifest.json"


def resource_manifest(datas: list[tuple[str, str]]) -> dict[str, str]:
    """Inventory the exact package data PyInstaller will copy, including directories."""
    manifest = {}
    for source, destination in datas:
        path = Path(source)
        files = sorted(path.rglob("*")) if path.is_dir() else [path]
        for file in files:
            if not file.is_file():
                continue
            relative = file.relative_to(path) if path.is_dir() else Path(file.name)
            target = (Path(destination) / relative).as_posix()
            manifest[target] = hashlib.sha256(file.read_bytes()).hexdigest()
    return dict(sorted(manifest.items()))


def validate_resources(bundle_root: Path) -> None:
    """Fail startup when extraction omitted or changed any inventoried resource."""
    manifest_path = bundle_root / "rcp" / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not manifest:
        raise RuntimeError("The packaged resource inventory is empty.")
    for relative, expected in manifest.items():
        path = bundle_root / relative
        if not path.is_file():
            raise RuntimeError(f"Missing packaged resource: {relative}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"Changed packaged resource: {relative}")
