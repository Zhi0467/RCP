import json
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

from rcp.frozen_resources import MANIFEST_NAME, resource_manifest


PROJECT_ROOT = Path(SPECPATH).parent
SOURCE_ROOT = PROJECT_ROOT / "src"
WEB_DIST = PROJECT_ROOT / "web" / "dist"
RUNTIME_HOOK = PROJECT_ROOT / "packaging" / "hooks" / "validate_frozen_resources.py"

# Source is runtime data: SSH helpers and inspect.getsource need the actual .py
# files, not just PyInstaller's importable bytecode. Collect the whole first-party
# package so new helpers, nested skill assets, and service templates ship by default.
package_data = collect_data_files("rcp", include_py_files=True)
datas = [(str(WEB_DIST), "rcp/web_dist"), *package_data]
manifest_dir = PROJECT_ROOT / "packaging" / "build" / "resources"
manifest_dir.mkdir(parents=True, exist_ok=True)
manifest_path = manifest_dir / MANIFEST_NAME
manifest_path.write_text(json.dumps(resource_manifest(datas), sort_keys=True), encoding="utf-8")
datas.append((str(manifest_path), "rcp"))

if not (WEB_DIST / "index.html").is_file():
    raise SystemExit("web/dist is missing; run the frontend build before PyInstaller")

analysis = Analysis(
    [str(SOURCE_ROOT / "rcp" / "__main__.py")],
    pathex=[str(SOURCE_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=collect_submodules("uvicorn"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(RUNTIME_HOOK)],
    excludes=["PyInstaller", "pytest", "ruff"],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="rcp-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="arm64",
    codesign_identity=None,
    entitlements_file=None,
)
