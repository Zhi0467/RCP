"""Shared validation for values passed to OpenSSH as destinations."""

from __future__ import annotations

import re

_SSH_DESTINATION = re.compile(r"[A-Za-z0-9_.@:+][A-Za-z0-9_.@:+-]{0,254}")


def validate_ssh_destination(host: str) -> str:
    """Reject option-shaped or shell-shaped values before invoking OpenSSH."""

    if not host or host.startswith("-") or _SSH_DESTINATION.fullmatch(host) is None:
        raise ValueError("SSH destination contains unsupported characters")
    return host
