"""Naming one provider failure so recovery can offer the right next step."""

from __future__ import annotations

import pytest

from rcp.agents.failure_kinds import classify_agent_failure, transport_failure
from rcp.providers import classify_terminal_error, profile_for

CODEX = profile_for("codex")

# The exact diagnostic a revoked Codex login produced on a real remote run.
REVOKED_LOGIN = (
    "Your access token could not be refreshed because your refresh token was revoked. "
    "Please log out and sign in again. ERROR codex_login::auth::manager: Failed to "
    'refresh token: 401 Unauthorized: {"error": {"code": "refresh_token_invalidated"}}'
)


@pytest.mark.parametrize("return_code", [255, -15])
def test_ssh_exit_codes_are_transport_failures_only_for_a_remote_run(return_code: int) -> None:
    assert transport_failure(return_code, "gpu.example.edu") is True
    # The same codes on a local run mean the provider itself, not a link.
    assert transport_failure(return_code, "") is False


def test_a_missing_exit_code_is_not_a_transport_failure() -> None:
    assert transport_failure(None, "gpu.example.edu") is False


def test_lost_connection_is_worth_another_attempt() -> None:
    assert (
        classify_agent_failure(
            error="The connection to gpu.example.edu was lost before codex finished.",
            return_code=255,
            host="gpu.example.edu",
            profile=CODEX,
            provider_spoke_for_itself=False,
        )
        == "transport_lost"
    )


@pytest.mark.parametrize(
    ("host", "link_lost", "expected"),
    [
        ("gpu.example.edu", True, "transport_lost"),
        ("gpu.example.edu", False, None),
        ("", True, None),
    ],
)
def test_a_link_lost_before_the_provider_started_has_no_code_to_read(
    host: str, link_lost: bool, expected: str | None
) -> None:
    """The readiness probe, the previous-pass check, and the input transfer all
    fail a turn before any provider process exists. Only their typed word can
    name the lost link then; the error text never does."""

    assert (
        classify_agent_failure(
            error=f"{host or 'laptop'} is unreachable, so codex could not be checked.",
            return_code=None,
            host=host,
            profile=CODEX,
            provider_spoke_for_itself=False,
            link_lost_before_provider=link_lost,
        )
        == expected
    )


def test_revoked_login_is_named_even_when_it_exits_like_a_dropped_link() -> None:
    """The provider's own diagnostic is more specific than an exit code, and a
    revoked login retried forever never recovers."""

    assert (
        classify_agent_failure(
            error=REVOKED_LOGIN,
            return_code=255,
            host="gpu.example.edu",
            profile=CODEX,
            provider_spoke_for_itself=False,
        )
        == "provider_auth"
    )


def test_an_ordinary_provider_failure_keeps_its_existing_behaviour() -> None:
    assert (
        classify_agent_failure(
            error="codex exited 2 on gpu.example.edu.",
            return_code=2,
            host="gpu.example.edu",
            profile=CODEX,
            provider_spoke_for_itself=False,
        )
        is None
    )


def test_a_profile_without_observed_signatures_does_not_guess() -> None:
    """A wrong credential match would withdraw Retry from a failure Retry fixes,
    so a profile with no real revoked login observed stays silent."""

    assert (
        classify_agent_failure(
            error=REVOKED_LOGIN,
            return_code=1,
            host="gpu.example.edu",
            profile=profile_for("claude"),
            provider_spoke_for_itself=False,
        )
        is None
    )


@pytest.mark.parametrize(
    ("spoke", "expected"),
    [(False, "transport_lost"), (True, None)],
)
def test_a_provider_that_spoke_for_itself_did_not_lose_its_link(
    spoke: bool, expected: str | None
) -> None:
    """ssh returns 255 for a provider that exits 255 as readily as for a lost link.

    Both shapes were observed on one real remote host: a turn that streamed its
    answers, reached its own terminal event and then exited 255 complaining that
    its thread was gone, and a turn whose stream simply stopped. Only what the
    provider managed to say separates them, and calling the first a lost link
    would reattempt work it had already done.
    """

    assert (
        classify_agent_failure(
            error="codex could not finish.",
            return_code=255,
            host="gpu.example.edu",
            profile=CODEX,
            provider_spoke_for_itself=spoke,
        )
        == expected
    )


def test_only_the_missing_thread_half_means_the_session_is_gone() -> None:
    """A vanished session sends recovery down the clean-start paths.

    That is right when the thread is gone and wrong for any other spawn
    failure, which still has its session: starting clean would throw away a
    live checkpoint and repeat the work it holds.
    """

    assert (
        classify_terminal_error(
            "collab spawn failed: no thread with id: 01a0976e-c283-7622-b2d6-43bf9d992198"
        )
        == "stale_session"
    )
    assert (
        classify_terminal_error("collab spawn failed: sandbox denied the request")
        == "provider_error"
    )


@pytest.mark.parametrize("provider, expected", [("codex", "provider_auth"), ("claude", None)])
def test_recorded_reused_refresh_login_is_provider_specific(provider, expected):
    diagnostic = (
        "stream error: unexpected status 401 Unauthorized: "
        '{"error":{"type":"refresh_token_reused",'
        '"message":"Your refresh token was already used. Please log in again."}}'
    )
    assert (
        classify_agent_failure(
            error=diagnostic,
            return_code=1,
            host="",
            profile=profile_for(provider),
            provider_spoke_for_itself=True,
        )
        == expected
    )
