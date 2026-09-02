from __future__ import annotations

from fastapi import HTTPException, Request, Response

TEAM_SHELL_PROTOCOL_HEADER = "RCP-Team-Shell-Protocol"
TEAM_SHELL_PROTOCOL_MINIMUM = 1
TEAM_SHELL_PROTOCOL_MAXIMUM = 1
TEAM_SHELL_PROTOCOL_MISMATCH_STATUS = 426
TEAM_SHELL_PROTOCOL_MISMATCH_CODE = "team_shell_protocol_mismatch"


def team_shell_protocol_range() -> dict[str, int]:
    return {
        "minimum": TEAM_SHELL_PROTOCOL_MINIMUM,
        "maximum": TEAM_SHELL_PROTOCOL_MAXIMUM,
    }


def acknowledge_team_shell_protocol(
    request: Request,
    response: Response,
    *,
    required_on_installed_server: bool = False,
) -> int | None:
    """Validate and echo a native-shell selection when the caller supplies one."""

    raw = request.headers.get(TEAM_SHELL_PROTOCOL_HEADER)
    if raw is None:
        metadata = request.app.state.instance_metadata
        if metadata.running_commit is None or not required_on_installed_server:
            return None
        raise HTTPException(
            status_code=TEAM_SHELL_PROTOCOL_MISMATCH_STATUS,
            detail={
                "code": TEAM_SHELL_PROTOCOL_MISMATCH_CODE,
                "message": "This installed team server requires a team-shell protocol selection.",
                "server_protocol": team_shell_protocol_range(),
                "action": "Update and rebuild RCP desktop from current origin/main.",
            },
        )
    try:
        selected = int(raw)
    except ValueError:
        selected = None
    if (
        selected is None
        or str(selected) != raw
        or not TEAM_SHELL_PROTOCOL_MINIMUM <= selected <= TEAM_SHELL_PROTOCOL_MAXIMUM
    ):
        raise HTTPException(
            status_code=TEAM_SHELL_PROTOCOL_MISMATCH_STATUS,
            detail={
                "code": TEAM_SHELL_PROTOCOL_MISMATCH_CODE,
                "message": "The selected team-shell protocol is not supported by this server.",
                "server_protocol": team_shell_protocol_range(),
                "action": "Update the RCP desktop or team server from current origin/main.",
            },
        )
    response.headers[TEAM_SHELL_PROTOCOL_HEADER] = raw
    return selected


__all__ = [
    "TEAM_SHELL_PROTOCOL_HEADER",
    "TEAM_SHELL_PROTOCOL_MAXIMUM",
    "TEAM_SHELL_PROTOCOL_MINIMUM",
    "TEAM_SHELL_PROTOCOL_MISMATCH_CODE",
    "TEAM_SHELL_PROTOCOL_MISMATCH_STATUS",
    "acknowledge_team_shell_protocol",
    "team_shell_protocol_range",
]
