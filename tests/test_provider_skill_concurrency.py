from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Literal

from rcp.agents import ProviderReadiness
from rcp.provider_skills import (
    ProviderSkillInventoryManager,
    ProviderSkillInventorySnapshot,
)
from rcp.storage import AppStore


def _ready() -> ProviderReadiness:
    return ProviderReadiness(
        provider="claude",
        installed=True,
        authenticated=True,
        version="provider 1.2.3",
        binary_path="/opt/claude",
        path_state="resolved",
    )


def _claude_output(*names: str) -> str:
    return json.dumps(
        {
            "type": "system",
            "subtype": "init",
            "skills": list(names),
        }
    )


def test_concurrent_refreshes_share_one_probe(tmp_path: Path, monkeypatch) -> None:
    manager = ProviderSkillInventoryManager(AppStore(tmp_path / "app.sqlite3"), timeout=1.0)
    manager.mark_refreshing("claude", "", "/opt/claude")

    first_probe_started = threading.Event()
    duplicate_probe_started = threading.Event()
    release_probe = threading.Event()
    probe_calls_lock = threading.Lock()
    probe_calls = 0

    def run_probe(
        _host: str,
        _command: list[str],
        _protocol: Literal["jsonrpc", "jsonl"],
    ) -> object:
        nonlocal probe_calls
        with probe_calls_lock:
            probe_calls += 1
            current_call = probe_calls
        if current_call == 1:
            first_probe_started.set()
        else:
            duplicate_probe_started.set()
        if not release_probe.wait(timeout=2.0):
            raise AssertionError("test did not release the provider probe")
        return _claude_output("review")

    monkeypatch.setattr(manager, "_run_probe", run_probe)
    owner_snapshots: list[ProviderSkillInventorySnapshot] = []
    owner_errors: list[Exception] = []

    def refresh_owner() -> None:
        try:
            owner_snapshots.append(
                manager.refresh("claude", "", "/opt/claude", _ready())
            )
        except Exception as exc:
            owner_errors.append(exc)

    owner_thread = threading.Thread(target=refresh_owner, daemon=True)
    owner_thread.start()
    owner_started = first_probe_started.wait(timeout=1.0)
    if not owner_started:
        release_probe.set()
        owner_thread.join(timeout=1.0)
    assert owner_started

    def release_after_overlap() -> None:
        duplicate_probe_started.wait(timeout=0.2)
        release_probe.set()

    releaser_thread = threading.Thread(target=release_after_overlap, daemon=True)
    releaser_thread.start()
    follower_snapshot = manager.refresh("claude", "", "/opt/claude", _ready())

    owner_thread.join(timeout=2.0)
    releaser_thread.join(timeout=2.0)

    assert not owner_thread.is_alive()
    assert not releaser_thread.is_alive()
    assert owner_errors == []
    assert probe_calls == 1
    assert not duplicate_probe_started.is_set()
    assert [snapshot.status for snapshot in owner_snapshots] == ["fresh"]
    assert follower_snapshot.status == "fresh"
    assert [skill.name for skill in follower_snapshot.skills] == ["review"]
