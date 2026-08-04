from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


PROJECT_ROOT = Path(SPECPATH).parent
SOURCE_ROOT = PROJECT_ROOT / "src"
WEB_DIST = PROJECT_ROOT / "web" / "dist"
RECORD_PARSER = SOURCE_ROOT / "rcp" / "sources" / "record_parsing.py"
SKILL_REGISTRY = SOURCE_ROOT / "rcp" / "skills"
RUNTIME_HOOK = PROJECT_ROOT / "packaging" / "hooks" / "validate_frozen_resources.py"

if not (WEB_DIST / "index.html").is_file():
    raise SystemExit("web/dist is missing; run the frontend build before PyInstaller")

analysis = Analysis(
    [str(SOURCE_ROOT / "rcp" / "__main__.py")],
    pathex=[str(SOURCE_ROOT)],
    binaries=[],
    datas=[
        (str(WEB_DIST), "rcp/web_dist"),
        (str(RECORD_PARSER), "rcp/sources"),
        (str(SKILL_REGISTRY), "rcp/skills"),
    ],
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
