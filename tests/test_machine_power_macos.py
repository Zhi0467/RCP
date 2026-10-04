"""The framework boundary is exercised on macOS; policy uses simulated readers."""

from __future__ import annotations

import subprocess
import sys

import pytest

from rcp.machine_power_macos import MacOSProfile


def test_import_on_non_darwin_does_not_load_frameworks():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import ctypes, sys; sys.platform = 'linux'; "
            "ctypes.CDLL = lambda *a, **k: sys.exit('loaded a framework'); "
            "from rcp.machine_power_macos import MacOSProfile; MacOSProfile()",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS frameworks")
def test_real_power_readers():
    profile = MacOSProfile()
    ac, capacity = profile.read_battery()
    assert type(ac) is bool
    assert capacity is None or (type(capacity) is int and 0 <= capacity <= 100)
    assert type(profile.read_thermal()) is bool
    assert type(profile.read_lid()) is bool
    assert type(profile.read_flag()) is bool
