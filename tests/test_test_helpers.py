from __future__ import annotations

from tests.helpers import wait_until


def test_wait_until_can_return_an_explicit_falsy_result() -> None:
    assert wait_until(lambda: 0, allow_falsy=True) == 0


def test_wait_until_keeps_boolean_predicates_pending_until_true() -> None:
    results = iter((False, True))

    assert wait_until(lambda: next(results)) is True
