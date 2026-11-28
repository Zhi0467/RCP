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


def test_agy_usage_probe_reads_all_groups() -> None:
    from rcp.providers.agy.profile import AgyProfile
    from rcp.providers.usage_limits import limit_evidence

    probe = AgyProfile().limit_probe("agy")
    assert probe.command == ["agy", "-p", "/usage", "--output-format", "json"]
    snapshot = probe.parser((FIXTURES / "agy.json").read_text())
    evidence = limit_evidence("agy", snapshot)
    assert evidence.exhausted
    assert evidence.resets_at == datetime(2026, 11, 28, 1, 15, 57, tzinfo=UTC)
    for group in snapshot["groups"]:
        for bucket in group["buckets"]:
            bucket["remaining_fraction"] = 0.5
    assert not limit_evidence("agy", snapshot).exhausted
    snapshot["groups"][-1]["buckets"][-1]["remaining_fraction"] = 0
    assert limit_evidence("agy", snapshot).resets_at == datetime(
        2026, 11, 28, 3, 20, 50, tzinfo=UTC
    )


def test_codex_usage_probe_reads_account_response() -> None:
    from rcp.providers.codex.profile import CodexProfile
    from rcp.providers.usage_limits import limit_evidence

    probe = CodexProfile().limit_probe("codex")
    messages = [json.loads(line) for line in probe.stdin.splitlines()]
    assert [message["method"] for message in messages] == [
        "initialize",
        "initialized",
        "account/rateLimits/read",
    ]
    snapshot = probe.parser(
        json.dumps(
            {
                "id": 2,
                "result": {
                    "rateLimits": {
                        "primary": {"usedPercent": 100, "resetsAt": FAILED_AT.timestamp() + 120}
                    }
                },
            }
        )
    )
    evidence = limit_evidence("codex", snapshot)
    assert evidence.exhausted
    assert evidence.resets_at == FAILED_AT + timedelta(seconds=120)
    with pytest.raises(ValueError, match="rateLimits"):
        probe.parser('{"id":2,"error":{"message":"unavailable"}}')


@pytest.mark.parametrize(
    "provider,snapshot",
    [
        ("codex", {"primary": {"usedPercent": 100}}),
        ("claude", {"status": "rejected"}),
        ("agy", {"groups": [{"buckets": [{"remaining_fraction": 0}]}]}),
    ],
)
def test_exhaustion_does_not_require_reset(provider, snapshot) -> None:
    from rcp.providers.usage_limits import limit_evidence, reset_from_evidence

    evidence = limit_evidence(provider, snapshot)
    assert evidence.exhausted
    assert evidence.resets_at is None
    reset = reset_from_evidence(evidence, failed_at=FAILED_AT)
    assert reset.estimated
    assert reset.resets_at == FAILED_AT + timedelta(hours=5)


def test_claude_has_no_usage_probe() -> None:
    assert ClaudeProfile().limit_probe("claude") is None
