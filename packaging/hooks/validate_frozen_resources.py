"""Validate the build-derived inventory instead of a second hand-written file list."""

import sys
from pathlib import Path

from rcp.frozen_resources import validate_resources

bundle_root = Path(sys._MEIPASS)
validate_resources(bundle_root)
if not (bundle_root / "rcp" / "machine_power_watchdog.sh").is_file():
    raise RuntimeError("Missing packaged resource: rcp/machine_power_watchdog.sh")
