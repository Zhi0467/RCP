"""Where members' own devices reach the team server.

The address is operator-set and read-only to members. An installed server
carries it as `access_url` in `/etc/rcp/team.toml`, beside server.toml; a
source-run server may set `RCP_TEAM_ACCESS_URL` instead. It is display
text with no authority: it tells a phone where the team space is.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from rcp.server_ops.config import load_team_access_config
from rcp.server_ops.layout import DEFAULT_SERVER_LAYOUT
from rcp.storage.models import normalize_space_access_url

TEAM_ACCESS_URL_ENV = "RCP_TEAM_ACCESS_URL"
INSTALLED_TEAM_CONFIG_PATH: Path = DEFAULT_SERVER_LAYOUT.team_config_path

logger = logging.getLogger(__name__)


def team_access_url() -> str | None:
    """Return the configured https origin, or None when unset or unusable."""

    override = os.environ.get(TEAM_ACCESS_URL_ENV)
    if override is not None:
        try:
            return normalize_space_access_url(override)
        except ValueError as exc:
            logger.warning("%s is not an https origin and is ignored: %s", TEAM_ACCESS_URL_ENV, exc)
            return None
    try:
        config = load_team_access_config(INSTALLED_TEAM_CONFIG_PATH)
    except (OSError, ValueError) as exc:
        logger.warning("team address file is unreadable; no team address: %s", exc)
        return None
    return config.access_url if config is not None else None


__all__ = ["INSTALLED_TEAM_CONFIG_PATH", "TEAM_ACCESS_URL_ENV", "team_access_url"]
