"""Fixed readiness slots shared by compute and server contracts."""

from __future__ import annotations

from typing import Literal

ComputeRoute = Literal["scheduler", "helper"]
