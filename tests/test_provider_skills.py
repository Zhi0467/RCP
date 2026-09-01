from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Literal

import pytest

from rcp.agents import ProviderReadiness
from rcp.provider_skills import (
    ProviderSkillInventoryManager,
    ProviderSkillInventorySnapshot,
)
from rcp.providers import ProviderSkillProbe, profile_for
from rcp.storage import AppStore
from tests.helpers import TASK_SETTLE_TIMEOUT


def _ready(provider: str, binary: str, version: str = "provider 1.2.3") -> ProviderReadiness:
    return ProviderReadiness(
        provider=provider,
        installed=True,
        authenticated=True,
        version=version,
        binary_path=binary,
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


def test_success_replaces_inventory_and_failure_preserves_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = AppStore(tmp_path / "app.sqlite3")
    manager = ProviderSkillInventoryManager(store)
    outputs = [_claude_output("review", "plugin:triage")]

    def run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 0, outputs.pop(0), "")

    monkeypatch.setattr(subprocess, "run", run)
    manager.mark_refreshing("claude", "", "/opt/claude")
    fresh = manager.refresh("claude", "", "/opt/claude", _ready("claude", "/opt/claude"))

    assert fresh.status == "fresh"
    assert [skill.name for skill in fresh.skills] == ["plugin:triage", "review"]
    assert fresh.command[0] == "/opt/claude"
    assert fresh.inventory_hash
    saved = fresh.model_copy(deep=True)

    def fail(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 7, "", "probe unavailable")

    monkeypatch.setattr(subprocess, "run", fail)
    manager.mark_refreshing("claude", "", "/opt/claude")
    stale = manager.refresh("claude", "", "/opt/claude", _ready("claude", "/opt/claude"))

    assert stale.status == "stale"
    assert stale.stale is True
    assert stale.diagnostic == "probe unavailable"
    assert stale.skills == saved.skills
    assert stale.provider_version == saved.provider_version
    assert stale.command == saved.command
    assert stale.inventory_hash == saved.inventory_hash
    references = manager.resolve("claude", "", "/opt/claude", "laptop", ["review", "plugin:triage"])
    assert [reference.name for reference in references] == ["review", "plugin:triage"]
    assert all(reference.stale for reference in references)


def test_first_failure_has_no_native_skills(tmp_path: Path) -> None:
    manager = ProviderSkillInventoryManager(AppStore(tmp_path / "app.sqlite3"))
    manager.mark_refreshing("codex", "gpu.example", "/opt/codex")
    snapshot = manager.refresh(
        "codex",
        "gpu.example",
        "/opt/codex",
        ProviderReadiness(
            provider="codex",
            installed=False,
            authenticated=False,
            path_state="unreachable",
            reason="gpu.example is unreachable",
        ),
    )

    assert snapshot.status == "unavailable"
    assert snapshot.skills == []
    assert snapshot.inventory_hash is None
    assert snapshot.diagnostic == "gpu.example is unreachable"


def test_concurrent_refreshes_share_owner_probe_and_readiness(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = ProviderSkillInventoryManager(AppStore(tmp_path / "app.sqlite3"), timeout=1.0)
    manager.mark_refreshing("claude", "", "/opt/claude")

    key = manager._key("claude", "", "/opt/claude")
    with manager._lock:
        pending = manager._pending_refreshes[key]
        pending.deadline = 0.0

    follower_wait_started = threading.Event()
    original_wait = pending.event.wait

    def observed_wait(timeout: float | None = None) -> bool:
        follower_wait_started.set()
        return original_wait(timeout)

    monkeypatch.setattr(pending.event, "wait", observed_wait)

    first_probe_started = threading.Event()
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
        first_probe_started.set()
        if not release_probe.wait(timeout=TASK_SETTLE_TIMEOUT):
            raise AssertionError("test did not release the provider probe")
        return _claude_output("review")

    monkeypatch.setattr(manager, "_run_probe", run_probe)
    owner_results: list[ProviderSkillInventorySnapshot] = []
    follower_results: list[ProviderSkillInventorySnapshot] = []
    errors: list[Exception] = []

    def refresh_into(
        results: list[ProviderSkillInventorySnapshot],
        readiness: ProviderReadiness,
    ) -> None:
        try:
            results.append(manager.refresh("claude", "", "/opt/claude", readiness))
        except Exception as exc:
            errors.append(exc)

    owner_thread = threading.Thread(
        target=refresh_into,
        args=(owner_results, _ready("claude", "/opt/claude")),
        daemon=True,
    )
    owner_thread.start()
    owner_started = first_probe_started.wait(timeout=TASK_SETTLE_TIMEOUT)
    if not owner_started:
        release_probe.set()
        owner_thread.join(timeout=TASK_SETTLE_TIMEOUT)
    assert owner_started

    with manager._lock:
        assert pending.owned is True
        assert pending.deadline > time.monotonic()

    follower_readiness = ProviderReadiness(
        provider="claude",
        installed=False,
        authenticated=False,
        path_state="missing",
        reason="the owner readiness must win",
    )
    follower_thread = threading.Thread(
        target=refresh_into,
        args=(follower_results, follower_readiness),
        daemon=True,
    )
    follower_thread.start()
    follower_waiting = follower_wait_started.wait(timeout=TASK_SETTLE_TIMEOUT)
    if not follower_waiting:
        release_probe.set()
        owner_thread.join(timeout=TASK_SETTLE_TIMEOUT)
        follower_thread.join(timeout=TASK_SETTLE_TIMEOUT)
    assert follower_waiting

    release_probe.set()
    owner_thread.join(timeout=TASK_SETTLE_TIMEOUT)
    follower_thread.join(timeout=TASK_SETTLE_TIMEOUT)

    assert not owner_thread.is_alive()
    assert not follower_thread.is_alive()
    assert errors == []
    assert probe_calls == 1
    assert len(owner_results) == 1
    assert len(follower_results) == 1
    assert owner_results[0] == follower_results[0]
    assert owner_results[0].status == "fresh"
    assert [skill.name for skill in owner_results[0].skills] == ["review"]


def test_concurrent_refresh_timeout_never_returns_refreshing(tmp_path: Path) -> None:
    manager = ProviderSkillInventoryManager(AppStore(tmp_path / "app.sqlite3"), timeout=0.0)
    manager.mark_refreshing("claude", "", "/opt/claude")

    with manager._lock:
        pending = manager._pending_refreshes[manager._key("claude", "", "/opt/claude")]
        pending.owned = True
        pending.deadline = 0.0

    with pytest.raises(TimeoutError, match="Timed out waiting for provider skill refresh"):
        manager.refresh("claude", "", "/opt/claude", _ready("claude", "/opt/claude"))

    assert manager.snapshot("claude", "", "/opt/claude", "local").status == "refreshing"


def test_codex_probe_waits_for_initialize_before_listing_skills(
    tmp_path: Path,
    monkeypatch,
) -> None:
    writes: list[dict[str, object]] = []

    class Stdin:
        def write(self, value: str) -> None:
            writes.append(json.loads(value))

        def flush(self) -> None:
            return None

        def close(self) -> None:
            return None

    class Process:
        stdin = Stdin()
        stdout = iter(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"userAgent": "codex"}}) + "\n",
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {
                            "data": [
                                {
                                    "skills": [
                                        {
                                            "name": "audit",
                                            "description": "Audit the graph",
                                            "enabled": True,
                                            "scope": "user",
                                            "path": "/skills/audit/SKILL.md",
                                            "interface": {"displayName": "Graph audit"},
                                        }
                                    ]
                                }
                            ]
                        },
                    }
                )
                + "\n",
            ]
        )
        stderr = iter([])

        def poll(self):
            return None

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return 0

        def kill(self):
            return None

    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: Process())
    manager = ProviderSkillInventoryManager(AppStore(tmp_path / "app.sqlite3"))
    manager.mark_refreshing("codex", "", "/opt/codex")
    snapshot = manager.refresh("codex", "", "/opt/codex", _ready("codex", "/opt/codex"))

    assert snapshot.status == "fresh"
    assert [skill.name for skill in snapshot.skills] == ["audit"]
    assert [(value.get("method"), value.get("id")) for value in writes] == [
        ("initialize", 1),
        ("initialized", None),
        ("skills/list", 2),
    ]
    assert writes[0]["params"] == {
        "clientInfo": {"name": "rcp", "version": "1"},
        "capabilities": {},
    }
    assert writes[-1]["params"] == {"cwds": ["/"], "forceReload": True}


def test_remote_probe_uses_existing_ssh_login_shell_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def run(arguments, **kwargs):
        captured["arguments"] = arguments
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(arguments, 0, _claude_output("research"), "")

    monkeypatch.setattr(subprocess, "run", run)
    manager = ProviderSkillInventoryManager(AppStore(tmp_path / "app.sqlite3"))
    manager.mark_refreshing("claude", "gpu.example", "/remote/claude")
    result = manager.refresh(
        "claude",
        "gpu.example",
        "/remote/claude",
        _ready("claude", "/remote/claude"),
    )

    arguments = captured["arguments"]
    assert arguments[0] == "ssh"
    assert "gpu.example" in arguments
    assert "bash -lic" in arguments[-1]
    assert "/remote/claude" in arguments[-1]
    assert result.status == "fresh"


def test_refresh_command_is_owned_by_provider_profile() -> None:
    codex = profile_for("codex").skill_probe("/opt/codex")
    claude = profile_for("claude").skill_probe("/opt/claude")

    assert codex == ProviderSkillProbe(command=["/opt/codex", "app-server"], protocol="jsonrpc")
    assert claude.command[0] == "/opt/claude"
    assert claude.protocol == "jsonl"
    assert "--no-session-persistence" in claude.command
    assert '{"disableAllHooks":true}' in claude.command
