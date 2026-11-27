from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rcp.limits import PROVIDER_LOGIN_LIMIT_HINT_SECONDS
from rcp.providers import classify_terminal_error
from rcp.providers.claude.profile import ClaudeProfile
from rcp.providers.usage_limits import limit_reset
from tests.test_codex_app_server import _accounting_turn

FAILED_AT = datetime(2026, 11, 27, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures" / "provider_usage_limits"


def test_gemini_captured_quota_error() -> None:
    error = (FIXTURES / "gemini.txt").read_text()
    assert classify_terminal_error(error) == "session_limit"
    reset = limit_reset("agy", None, error, failed_at=FAILED_AT)
    assert reset.resets_at == FAILED_AT + timedelta(hours=2, minutes=57, seconds=1)
    assert not reset.estimated


def test_codex_exhausted_windows_use_latest_reset() -> None:
    snapshot: dict[str, object] = {
        "primary": {"usedPercent": 100, "resetsAt": FAILED_AT.timestamp() + 120},
        "secondary": {"usedPercent": 100, "resetsAt": FAILED_AT.timestamp() + 240},
    }
    reset = limit_reset("codex", snapshot, "try again at 2026-11-28T00:00:00Z", failed_at=FAILED_AT)
    assert reset.resets_at == FAILED_AT + timedelta(seconds=240)
    assert not reset.estimated


def test_claude_captured_rate_event_is_preserved() -> None:
    value = json.loads((FIXTURES / "claude.json").read_text())
    event = ClaudeProfile().decode_event(value, json.dumps(value))
    assert event.event == "rate_limit"
    assert event.rate_limit == value["rate_limit_info"]
    failed_at = datetime.fromtimestamp(1795820000, UTC)
    value["rate_limit_info"]["unifiedWindows"]["five_hour"]["status"] = "rejected"
    reset = limit_reset("claude", value["rate_limit_info"], "", failed_at=failed_at)
    assert reset.resets_at == datetime.fromtimestamp(1795824600, UTC)
    assert not reset.estimated


@pytest.mark.parametrize(
    "error", ["try again at 2026-11-27T01:00:00Z", "try again at 2026-11-27 01:00:00 UTC"]
)
def test_codex_text_reset(error: str) -> None:
    reset = limit_reset("codex", None, error, failed_at=FAILED_AT)
    assert reset.resets_at == FAILED_AT + timedelta(hours=1)
    assert not reset.estimated


@pytest.mark.parametrize(
    "snapshot,error",
    [
        (None, "unknown"),
        ({"primary": {"usedPercent": 100, "resetsAt": "invalid"}}, ""),
        (
            {"primary": {"usedPercent": 100, "resetsAt": FAILED_AT.timestamp() + 59}},
            "try again at 2026-11-28T00:00:00Z",
        ),
        ({"primary": {"usedPercent": 10, "resetsAt": FAILED_AT.timestamp() + 120}}, ""),
    ],
)
def test_unknown_or_too_soon_uses_estimate(snapshot, error: str) -> None:
    reset = limit_reset("codex", snapshot, error, failed_at=FAILED_AT)
    assert reset.resets_at == FAILED_AT + timedelta(seconds=PROVIDER_LOGIN_LIMIT_HINT_SECONDS)
    assert reset.estimated


def test_codex_clock_without_timezone_is_unknown() -> None:
    reset = limit_reset("codex", None, "Try again at 4:10 PM", failed_at=FAILED_AT)
    assert reset.estimated
    assert reset.resets_at == FAILED_AT + timedelta(seconds=PROVIDER_LOGIN_LIMIT_HINT_SECONDS)


def test_minimum_reset_boundary_is_known() -> None:
    reset = limit_reset("agy", None, "Resets in 0h1m0s", failed_at=FAILED_AT)
    assert not reset.estimated
    assert reset.resets_at == FAILED_AT + timedelta(minutes=1)


def test_claude_allowed_windows_do_not_name_a_reset_for_failure() -> None:
    value = json.loads((FIXTURES / "claude.json").read_text())
    reset = limit_reset(
        "claude", value["rate_limit_info"], "", failed_at=datetime.fromtimestamp(1795820000, UTC)
    )
    assert reset.estimated


def test_app_server_keeps_account_rate_limit_snapshot(tmp_path: Path) -> None:
    turn = _accounting_turn(tmp_path, resumed=False)
    fixture = json.loads((FIXTURES / "codex.json").read_text())
    step = turn.receive_line(json.dumps(fixture["notification"]))
    assert len(step.events) == 1
    assert step.events[0].event == "rate_limit"
    assert step.events[0].rate_limit == fixture["notification"]["params"]["rateLimits"]
    failed = turn.receive_line(json.dumps(fixture["error"]))
    assert failed.events[0].event == "error"

    reset = limit_reset(
        "codex", None, failed.events[0].text, failed_at=datetime(2026, 11, 27, tzinfo=UTC)
    )
    assert reset.resets_at == datetime(2026, 11, 27, 1, tzinfo=UTC)
    assert not reset.estimated


def test_codex_offset_text_is_normalized_to_utc() -> None:
    reset = limit_reset(
        "codex", None, "Try again at 2026-11-27T03:00:00+02:00", failed_at=FAILED_AT
    )
    assert reset.resets_at.tzinfo == UTC
    assert reset.resets_at == FAILED_AT + timedelta(hours=1)
