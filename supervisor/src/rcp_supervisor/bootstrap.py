"""Fresh-team initialization boundary after trusted paired-wheel host bootstrap."""

from __future__ import annotations

import os
import pwd
import tomllib
from pathlib import Path

from rcp_supervisor.errors import SupervisorError
from rcp_supervisor.launch import validate_selected_receipt
from rcp_supervisor.limits import MAX_SELECTED_RECEIPT_BYTES
from rcp_supervisor.runtime import DEFAULT_PATHS, Paths, _read_file


def bootstrap(
    bundle: Path,
    *,
    team_name: str,
    release: dict,
    paths: Paths = DEFAULT_PATHS,
) -> dict:
    """Validate installed integration and return the one-time secret ceremony.

    This does not start an app, select a release, or open SQLite. The root driver
    publishes its initial selected receipt before presenting the init command.
    That command inherits the operator's terminal, keeping the one-time bootstrap
    secret out of the supervisor's event stream and retained subprocess logs.
    """
    del bundle  # The driver already sealed and installed the original bundle.
    if os.geteuid() != 0:
        raise SupervisorError("Server installation requires root.")
    validate_selected_receipt(release, releases_root=paths.releases_root)
    if (
        not isinstance(team_name, str)
        or not team_name.strip()
        or len(team_name) > 160
        or any(ord(c) < 32 for c in team_name)
    ):
        raise SupervisorError("Team name must be one bounded nonempty line.")
    account = pwd.getpwnam(paths.service_account)
    if account.pw_uid == 0 or account.pw_gid == 0 or account.pw_dir != str(paths.service_home):
        raise SupervisorError("The dedicated unprivileged service account is not installed.")
    config = tomllib.loads(
        _read_file(paths.config, uid=0, max_bytes=MAX_SELECTED_RECEIPT_BYTES).decode()
    )
    if (
        config.get("schema_version") != 3
        or config.get("service_account") != paths.service_account
        or config.get("service_unit") != paths.service_unit
    ):
        raise SupervisorError(
            "Run the trusted paired-wheel bootstrap to converge installed configuration."
        )
    declared = config.get("paths", {})
    for name, expected in (
        ("config_path", paths.config),
        ("data_dir", paths.data_dir),
        ("current_release", paths.current),
        ("releases_root", paths.releases_root),
    ):
        if declared.get(name) != str(expected):
            raise SupervisorError("Installed configuration disagrees with the fixed server paths.")
    from rcp_supervisor.runtime import SystemRuntime

    state = SystemRuntime(paths).application(
        release, "inspect", {"version": 1, "data_dir": str(paths.data_dir)}
    )
    if state.get("version") != 1 or state.get("status") not in {
        "initialized_team",
        "uninitialized",
    }:
        raise SupervisorError(
            "The selected application did not prove its team initialization state."
        )
    if state["status"] == "initialized_team":
        return {"status": "ready", "actions": [], "resume_argv": []}
    initialize = [
        "sudo",
        "-u",
        paths.service_account,
        "-H",
        "/usr/local/bin/rcp",
        "space",
        "init",
        "--team",
        "--name",
        team_name,
    ]
    return {
        "status": "operator_action_needed",
        "actions": [{"kind": "command", "argv": initialize}],
        "resume_argv": [
            "sudo",
            "/usr/local/bin/rcp",
            "server",
            "install",
            "--team-name",
            team_name,
        ],
    }
