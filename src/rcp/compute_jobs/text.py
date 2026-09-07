"""The non-secret metadata and diagnostic boundary for compute jobs."""

from __future__ import annotations

import re

from rcp.limits import COMPUTE_JOB_DIAGNOSTIC_MAX_CHARS
from rcp.server_ops.models import redact_server_text

COMPUTE_CREDENTIAL_PATH = re.compile(
    r"(?i)(?:\.ssh[/\\]|(?:^|[/\\])id_(?:rsa|dsa|ecdsa|ed25519)(?:$|[\s,;])|"
    r"identity[_ -]?file)"
)


def validate_compute_metadata(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("compute metadata must be one line")
    if redact_server_text(value) != value or COMPUTE_CREDENTIAL_PATH.search(value):
        raise ValueError("compute metadata cannot contain credential-shaped text or paths")
    return value


def safe_compute_diagnostic(value: str) -> str:
    return " ".join(redact_server_text(value).split())[:COMPUTE_JOB_DIAGNOSTIC_MAX_CHARS]
