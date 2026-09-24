"""Expected failures reported by the independent supervisor."""

from __future__ import annotations

import re

from rcp_supervisor.limits import MAX_DIAGNOSTIC_CHARS


def safe_diagnostic(message: str) -> str:
    """Bound operator text without exposing credential-shaped application output."""
    message = re.sub(
        r"-----BEGIN [^-\n]*PRIVATE KEY-----.*?-----END [^-\n]*PRIVATE KEY-----",
        "[REDACTED]",
        message,
        flags=re.DOTALL,
    )
    message = re.sub(
        r"(?i)\b(?:Bearer\s+\S+|Basic\s+[A-Za-z0-9+/=]+|"
        r"rcp_(?:bootstrap|member)_[\w.-]+|(?:gh[pousr]_|github_pat_)[\w]+|"
        r"AGE-SECRET-KEY-1[A-Z0-9]+|sk-(?:ant-)?[\w-]+|"
        r"AKIA[0-9A-Z]+|xox[baprs]-[\w-]+)",
        "[REDACTED]",
        message,
    )
    message = re.sub(
        r"(?i)\b(password|passphrase|secret|token|authorization|private[_ -]?key|"
        r"api[_ -]?key|credential|cookie)(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        r"\1\2[REDACTED]",
        message,
    )
    return " ".join("".join(c if c.isprintable() else " " for c in message).split())[
        :MAX_DIAGNOSTIC_CHARS
    ]


class SupervisorError(RuntimeError):
    """An actionable supervisor failure, suitable for the operator event stream."""


class ApplicationCommandError(SupervisorError):
    """A failed command with bounded stdout available to its result decoder."""

    def __init__(self, message: str, output: bytes) -> None:
        super().__init__(message)
        self.output = output
