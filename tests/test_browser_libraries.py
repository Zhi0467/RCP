from __future__ import annotations

from pathlib import Path

import pytest

from rcp.browser import libraries
from rcp.server_ops.backup_models import inspect_app_data_capture_plan


def test_library_mapping_and_exact_repair_command() -> None:
    misses = "libasound.so.2 => not found\nlibnss3.so => not found\nlibnssutil3.so => not found\n"
    packages = libraries.missing_library_packages(misses, "24.04")
    assert packages == ("libasound2t64", "libnss3")
    assert libraries.apt_install_command(packages).split() == [
        "sudo",
        "apt-get",
        "install",
        "-y",
        "--no-install-recommends",
        *packages,
    ]
    assert "libasound2" in libraries.chromium_packages("22.04")
    with pytest.raises(ValueError):
        libraries.missing_library_packages("unknown.so => not found", "24.04")
    with pytest.raises(ValueError):
        libraries.chromium_packages("26.04")


def test_root_library_install_uses_fixed_packages_and_bounded_process(monkeypatch) -> None:
    monkeypatch.setattr(libraries.os, "geteuid", lambda: 0)
    monkeypatch.setattr(Path, "read_text", lambda _self: 'ID=ubuntu\nVERSION_ID="24.04"\n')
    observed = []
    monkeypatch.setattr(
        libraries.subprocess, "run", lambda argv, **kwargs: observed.append((argv, kwargs))
    )
    libraries.install_system_libraries()
    argv, kwargs = observed.pop()
    assert argv == (
        "/usr/bin/apt-get",
        "install",
        "-y",
        "--no-install-recommends",
        *libraries.chromium_packages("24.04"),
    )
    assert kwargs["check"] and kwargs["timeout"] > 0
    assert kwargs["env"]["DEBIAN_FRONTEND"] == "noninteractive"
    monkeypatch.setattr(libraries.os, "geteuid", lambda: 1000)
    with pytest.raises(PermissionError):
        libraries.install_system_libraries()
    assert not observed


def test_browser_storage_is_excluded_without_hiding_unknown_roots(tmp_path) -> None:
    for name in ("browser", "tools", "unknown"):
        (tmp_path / name).mkdir()
    result = inspect_app_data_capture_plan(tmp_path)
    assert {"browser", "tools"} <= set(result.excluded_entries)
    assert result.unclassified_entries == ("unknown",)
