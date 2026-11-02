from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from rcp.agents.command_protocol import validate_command_request
from rcp.agents.staged_command_client import ClientInputError, _parser


def test_legacy_lesson_command_is_rejected() -> None:
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        validate_command_request(
            json.dumps(
                {
                    "verb": "lesson",
                    "arguments": {"action": "list"},
                    "request_id": "a" * 32,
                    "mailbox_id": "b" * 32,
                    "credential": "c" * 64,
                }
            )
        )
    with pytest.raises(ClientInputError, match="invalid choice"):
        _parser().parse_args(
            ["--credential", "c" * 64, "--timeout", "1", "--workspace", ".", "lesson", "list"]
        )
