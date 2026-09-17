"""What a restart does to a turn whose provider is still out there.

Closing the laptop kills RCP, not the provider. The sweep that runs on the way
back up decides whether that turn's result is still reachable, so this is the
exact seam the whole design turns on.
"""

from __future__ import annotations

import pytest

from rcp.storage import AppStore

from .test_remote_provider_receipts import _store


@pytest.fixture
def store(tmp_path):
    return _store(tmp_path)


def _reopened(tmp_path) -> AppStore:
    """The store as the next RCP process finds it."""

    return AppStore(tmp_path / "state.sqlite3")


def test_a_supervised_pass_survives_the_restart_that_ended_its_watcher(tmp_path, store) -> None:
    store.begin_remote_provider_pass("first", "remote", "/stage", "/stage/one.pid", supervised=True)
    reopened = _reopened(tmp_path)

    reopened.interrupt_active_agent_tasks()

    task = reopened.agent_task("first")
    assert task.status not in {"interrupted", "failed"}
    assert task.phase == "awaiting_remote_result"
    assert "Waiting for the remote result" in (task.status_message or "")


def test_an_unsupervised_pass_is_interrupted_as_before(tmp_path, store) -> None:
    """Nothing wrote that turn down, so nothing can go back for it."""

    store.begin_remote_provider_pass("first", "remote", "/stage", "/stage/one.pid")
    reopened = _reopened(tmp_path)

    reopened.interrupt_active_agent_tasks()

    assert reopened.agent_task("first").status == "interrupted"


def test_a_pass_already_confirmed_stopped_is_interrupted(tmp_path, store) -> None:
    """Its host was asked and answered, so there is nothing left to wait on."""

    store.begin_remote_provider_pass("first", "remote", "/stage", "/stage/one.pid", supervised=True)
    store.finish_remote_provider_pass("first", "/stage/one.pid")
    reopened = _reopened(tmp_path)

    reopened.interrupt_active_agent_tasks()

    assert reopened.agent_task("first").status == "interrupted"
