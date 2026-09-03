from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "packaging" / "release_build.py"
SPEC = importlib.util.spec_from_file_location("release_build", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
release_build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_build)


def test_stamp_version_stamps_plain_version(tmp_path: Path) -> None:
    version_file = tmp_path / "__init__.py"
    version_file.write_text('"""RCP."""\n\n__version__ = "0.3.2"\n', encoding="utf-8")

    stamped = release_build.stamp_version(
        "412",
        "FE06636abcde0123456789abcdef0123456789ab",
        version_file=version_file,
    )

    assert stamped == "0.3.2+build.412.gfe06636"
    assert '__version__ = "0.3.2+build.412.gfe06636"' in version_file.read_text(encoding="utf-8")


def test_stamp_version_refuses_existing_local_segment(tmp_path: Path) -> None:
    version_file = tmp_path / "__init__.py"
    original = '__version__ = "0.3.2+build.411.g0123456"\n'
    version_file.write_text(original, encoding="utf-8")

    with pytest.raises(release_build.ReleaseBuildError, match="already has a local segment"):
        release_build.stamp_version(
            "412",
            "fe06636abcde0123456789abcdef0123456789ab",
            version_file=version_file,
        )

    assert version_file.read_text(encoding="utf-8") == original


def test_manifest_round_trip_detects_tampered_asset(tmp_path: Path) -> None:
    (tmp_path / "a.whl").write_bytes(b"wheel")
    (tmp_path / "requirements.lock.txt").write_bytes(b"locked")

    manifest = release_build.write_manifest(tmp_path, Path("manifest.sha256"))

    assert manifest.read_text(encoding="utf-8").splitlines() == sorted(
        manifest.read_text(encoding="utf-8").splitlines(),
        key=lambda line: line.split("  ", maxsplit=1)[1],
    )
    release_build.verify_manifest(tmp_path, Path("manifest.sha256"))

    (tmp_path / "a.whl").write_bytes(b"Wheel")
    with pytest.raises(release_build.ReleaseBuildError, match="a.whl.*mismatch"):
        release_build.verify_manifest(tmp_path, Path("manifest.sha256"))


def test_verify_manifest_accepts_manifest_outside_asset_directory(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "a.whl").write_bytes(b"wheel")
    manifest = release_build.write_manifest(assets, tmp_path / "manifest.sha256")

    release_build.verify_manifest(assets, manifest)


def test_verify_manifest_reports_missing_listed_asset(tmp_path: Path) -> None:
    asset = tmp_path / "a.whl"
    asset.write_bytes(b"wheel")
    release_build.write_manifest(tmp_path, Path("manifest.sha256"))
    asset.unlink()

    with pytest.raises(release_build.ReleaseBuildError) as error:
        release_build.verify_manifest(tmp_path, Path("manifest.sha256"))

    assert str(error.value) == "asset a.whl is missing"


def test_verify_manifest_reports_extra_unlisted_asset(tmp_path: Path) -> None:
    (tmp_path / "a.whl").write_bytes(b"wheel")
    release_build.write_manifest(tmp_path, Path("manifest.sha256"))
    (tmp_path / "extra.whl").write_bytes(b"extra")

    with pytest.raises(release_build.ReleaseBuildError) as error:
        release_build.verify_manifest(tmp_path, Path("manifest.sha256"))

    assert str(error.value) == "asset extra.whl is not listed in manifest"


def test_promotion_accepts_matching_base_version() -> None:
    release_build.check_promotion(Path("rcp-0.3.2+build.412.gfe06636-py3-none-any.whl"), "v0.3.2")


def test_promotion_refuses_mismatched_base_version() -> None:
    with pytest.raises(
        release_build.ReleaseBuildError,
        match=(
            r"build 412 has base version 0\.3\.2 but tag v0\.4\.0 was requested; "
            r"bump src/rcp/__init__\.py first"
        ),
    ):
        release_build.check_promotion(
            Path("rcp-0.3.2+build.412.gfe06636-py3-none-any.whl"), "v0.4.0"
        )


def test_promotion_refuses_invalid_tag() -> None:
    with pytest.raises(release_build.ReleaseBuildError, match="invalid release tag 0.3.2"):
        release_build.check_promotion(
            Path("rcp-0.3.2+build.412.gfe06636-py3-none-any.whl"), "0.3.2"
        )


def test_select_stale_builds_selects_only_expired_prerelease_builds(tmp_path: Path) -> None:
    releases_file = tmp_path / "releases.json"
    releases_file.write_text(
        json.dumps(
            [
                {
                    "tagName": "v0.3.2",
                    "isPrerelease": False,
                    "createdAt": "2026-06-01T00:00:00Z",
                },
                {
                    "tagName": "build/413",
                    "isPrerelease": True,
                    "createdAt": "2026-08-20T00:00:00Z",
                },
                {
                    "tagName": "build/412",
                    "isPrerelease": True,
                    "createdAt": "2026-07-01T00:00:00Z",
                },
            ]
        ),
        encoding="utf-8",
    )

    assert release_build.select_stale_builds(
        releases_file, "2026-09-02T00:00:00Z", release_build.STALE_BUILD_DAYS
    ) == ["build/412"]
