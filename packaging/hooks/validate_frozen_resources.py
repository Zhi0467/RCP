"""Validate the build-derived inventory instead of a second hand-written file list."""

import sys
from pathlib import Path

from rcp.frozen_resources import validate_resources

validate_resources(Path(sys._MEIPASS))
