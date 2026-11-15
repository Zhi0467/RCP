from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from rcp.agents.provider_environment import unhidden_read_scope
from rcp.machine_cards import BACKEND_MACHINE_ID
from rcp.storage import AppStore
from rcp.storage.models import AgentTaskRecord
from rcp.transport import remote_stage_root
from rcp.transport.run_stage import RemoteRunStage

from .test_provider_login_launch import _launch
from .test_provider_login_resolver import _login
from .test_provider_login_resolver import accounts as accounts

STATUSES = ("queued", "running", "pausing", "paused", "succeeded", "failed", "interrupted")


def _fingerprint(index: int) -> str:
    return f"{index:064x}"


def _store_with_launches(tmp_path: Path) -> AppStore:
    store = AppStore(tmp_path / "state.sqlite3")
    for index, status in enumerate(STATUSES):
        store.create_agent_task(
            AgentTaskRecord(
                operation_id=status,
                project_id="project",
                kind="seed",
                status="queued",
                request={},
                created_at=store.now(),
                updated_at=store.now(),
                status_message="Queued",
            )
        )
        store.record_agent_task_hidden_read(status, _fingerprint(index))
        with store.connection() as connection:
            connection.execute(
                "UPDATE graph_runs SET status = ? WHERE operation_id = ?", (status, status)
            )
    return store


def test_live_launches_protect_their_hidden_read_fingerprints(tmp_path: Path) -> None:
    store = _store_with_launches(tmp_path)

    assert set(store.protected_hidden_read_fingerprints()) == {
        _fingerprint(index) for index in range(4)
    }


@pytest.mark.asyncio
async def test_a_launch_records_the_policy_it_runs_under(accounts, tmp_path: Path) -> None:
    await _launch(accounts, tmp_path, _login(accounts, "codex", machine=True))

    assert (
        accounts.store.agent_task("current").hidden_read_fingerprint
        == unhidden_read_scope(machine_id=BACKEND_MACHINE_ID).fingerprint
    )


def _hidden_read_folders(home: Path, names: dict[str, float]) -> Path:
    parent = home / ".rcp" / "hidden-read"
    for name, age_days in names.items():
        folder = parent / name
        folder.mkdir(parents=True)
        wrapper = folder / "rcp-hidden-read.py"
        wrapper.write_text("", encoding="utf-8")
        wrapper.chmod(0o500)
        stamp = time.time() - age_days * 86400
        os.utime(folder, (stamp, stamp))
    return parent


def _names(old: str, kept: str, fresh: str) -> dict[str, float]:
    return {old: 30, kept: 30, fresh: 0, "not-a-fingerprint": 30}


def test_the_local_sweep_removes_only_old_unprotected_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve() / "home"
    monkeypatch.setenv("HOME", str(home))
    old, kept, fresh = (_fingerprint(index) for index in range(3))
    parent = _hidden_read_folders(home, _names(old, kept, fresh))
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = _fingerprint(9)
    (parent / linked).symlink_to(outside)

    remote_stage_root.sweep_hidden_reads(7, {kept})

    assert sorted(path.name for path in parent.iterdir()) == sorted(
        [kept, fresh, "not-a-fingerprint", linked]
    )
    assert outside.is_dir()


def test_a_remote_stage_sweep_carries_the_protected_fingerprints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shipped script runs in-process, so `/tmp` stands in for the legacy parent."""
    monkeypatch.setattr(remote_stage_root, "LEGACY_PARENT", str(tmp_path / "legacy-tmp"))
    home = tmp_path.resolve() / "home"
    monkeypatch.setenv("HOME", str(home))
    old, kept, fresh = (_fingerprint(index) for index in range(3))
    parent = _hidden_read_folders(home, _names(old, kept, fresh))
    stage = RemoteRunStage("research.example")
    calls: list[list[str]] = []

    def run_shipped(arguments: list[str]):
        calls.append(arguments)
        assert arguments[:3] == ["python3", "-I", "-c"]
        assert remote_stage_root.main(["remote_stage_root.py", *arguments[4:]]) == 0

    monkeypatch.setattr(stage, "_ssh", run_shipped)

    stage.sweep(retain_days=7, protected_roots=[])
    assert len(list(parent.iterdir())) == 4

    stage.sweep(retain_days=7, protected_roots=[], protected_hidden_reads=[kept])

    assert sorted(path.name for path in parent.iterdir()) == sorted(
        [kept, fresh, "not-a-fingerprint"]
    )
    assert len(calls) == 2


def test_a_remote_sweep_refuses_a_malformed_fingerprint() -> None:
    with pytest.raises(ValueError):
        RemoteRunStage("research.example").sweep(protected_hidden_reads=["../stages"])


def test_install_touches_its_folder_before_a_sweep_can_age_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rcp.agents.staged_hidden_read import install

    home = tmp_path.resolve() / "home"
    monkeypatch.setenv("HOME", str(home))
    name = _fingerprint(1)
    parent = _hidden_read_folders(home, {name: 30})
    for path in (home / ".rcp", parent, parent / name):
        path.chmod(0o700)
    stamp = time.time() - 30 * 86400
    os.utime(parent / name, (stamp, stamp))

    install(parent / name, {})
    remote_stage_root.sweep_hidden_reads(7, set())

    assert (parent / name).is_dir()
