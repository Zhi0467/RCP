from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from PyInstaller.utils.hooks import collect_data_files

from rcp.frozen_resources import MANIFEST_NAME, resource_manifest, validate_resources


def test_package_collection_includes_every_source_and_asset() -> None:
    """New nested helpers/assets must ship without a hand-maintained allowlist."""
    import rcp

    root = Path(rcp.__file__).parent
    collected = resource_manifest(collect_data_files("rcp", include_py_files=True))
    expected = {
        "rcp/" + path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }
    assert expected <= collected.keys()


@pytest.mark.parametrize("fault", [None, "missing", "changed"])
def test_inventory_checks_nested_sources_and_assets(tmp_path: Path, fault: str | None) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "new-helper.py").write_text("print('hello')\n")
    (source / "nested").mkdir()
    (source / "nested" / "template.service").write_text("[Service]\n")
    datas = [(str(source), "rcp/new-component")]
    manifest = resource_manifest(datas)
    bundle = tmp_path / "bundle"
    shutil.copytree(source, bundle / "rcp/new-component")
    (bundle / "rcp" / MANIFEST_NAME).write_text(json.dumps(manifest))
    target = bundle / "rcp/new-component/nested/template.service"
    if fault == "missing":
        target.unlink()
    elif fault == "changed":
        target.write_text("wrong data")
    if fault:
        with pytest.raises(
            RuntimeError, match="packaged resource: rcp/new-component/nested/template"
        ):
            validate_resources(bundle)
    else:
        validate_resources(bundle)
