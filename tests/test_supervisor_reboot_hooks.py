from __future__ import annotations

import io
import json
import os
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

import pytest
from rcp_supervisor import checkpoint

from tests import supervisor_reboot_guest as guest
from tests.supervisor_reboot_live import scenarios


def test_guest_fault_wrapper_interrupts_only_after_each_durable_root_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    live = tmp_path / "server"
    live.mkdir(mode=0o700)
    payload = tmp_path / "payload"
    payload.mkdir(mode=0o700)
    roots = []
    for name in ("data", "research"):
        directory = live / name
        directory.mkdir(mode=0o700)
        (directory / "retained").write_bytes(b"original\n")
        shutil.copytree(directory, payload / name)
        roots.append(checkpoint.SnapshotRoot(directory, payload / name))
    saved = checkpoint.create_checkpoint(
        tmp_path / "checkpoint", tuple(roots), boundary_sha256="a" * 64
    )
    request = {**asdict(saved), "directory": str(saved.directory)}
    for root in roots:
        (root.live / "retained").write_bytes(b"candidate\n")
    observed = []

    def boundary(name):
        phase, index = name.rsplit(":", 1)
        root = roots[int(index)]
        if phase == "candidate_root_quarantined":
            assert not root.live.exists()
        else:
            assert (root.live / "retained").read_bytes() == b"original\n"
        observed.append(name)

    monkeypatch.setattr(guest, "boundary", boundary)
    # The actual wrapper exits after one worker operation. Restore its process
    # instrumentation explicitly here because this test shares an interpreter.
    monkeypatch.setattr(os, "replace", os.replace)
    monkeypatch.setattr(checkpoint, "_fsync_directory", checkpoint._fsync_directory)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(request).encode())))
    assert guest.filesystem("restore", "candidate_") == 0
    assert json.loads(capsys.readouterr().out)["status"] == "restored"
    assert observed == [
        "candidate_root_quarantined:0",
        "candidate_root_published:0",
        "candidate_root_quarantined:1",
        "candidate_root_published:1",
    ]


def test_reboot_scenarios_cover_candidate_publication_repeated_rollback_and_admission() -> None:
    cases = scenarios()
    assert len({case.name for case in cases}) == len(cases)
    for offline in (False, True):
        selected = [case for case in cases if case.offline == offline]
        for kind in ("update", "restore"):
            assert any(
                case.kind == kind and case.pauses == ("candidate_verified", "root_quarantined:0")
                for case in selected
            )
            assert any(
                case.kind == kind and case.pauses == ("candidate_chosen",) for case in selected
            )
            assert any(case.kind == kind and case.force_rollback for case in selected)
        assert any(
            case.kind == "restore" and "candidate_root_published:1" in case.pauses
            for case in selected
        )
        assert any(case.pauses == ("post_admission",) for case in selected)
        assert any(case.kind == "invalid" for case in selected)
        assert {case.pauses for case in selected if case.kind == "fresh_restore"} == {
            ("candidate_root_published:1",),
            ("candidate_chosen",),
        }


def test_bootstrap_checks_production_doctor_before_qualification_dropin(tmp_path, monkeypatch):
    from types import SimpleNamespace

    bundle = tmp_path / "bundles/base"
    bundle.mkdir(parents=True)
    (bundle / "rcp-base.whl").touch()
    (bundle / "rcp_supervisor-base.whl").touch()
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    dropin = tmp_path / "rcp.service.d"
    calls = []
    monkeypatch.setattr(guest, "ROOT", tmp_path)
    monkeypatch.setattr(
        guest,
        "Path",
        lambda value: dropin if value == "/etc/systemd/system/rcp.service.d" else Path(value),
    )

    def run(argv, **kwargs):
        if argv[-1] == "setup":
            calls.append("setup")
            return SimpleNamespace(stdout='{"status": "installed"}')
        if argv[-1] == "setup-data":
            assert argv == [guest.SUPERVISOR_PYTHON, str(guest.SCRIPT), "setup-data"]
            assert not bootstrap.exists()
            calls.append("installed setup")
            return SimpleNamespace(stdout='{"status": "ready"}')
        if argv == ["systemctl", "daemon-reload"]:
            assert (dropin / "qualification.conf").is_file()
            calls.append("dropin")
        return SimpleNamespace(stdout="")

    def service(argv):
        assert argv == ["/usr/local/bin/rcp", "server", "doctor", "--machine-readable"]
        assert not dropin.exists()
        assert not bootstrap.exists()
        calls.append("doctor")

    monkeypatch.setattr(guest, "run", run)
    monkeypatch.setattr(guest, "service", service)
    assert guest.bootstrap()["temporary_bootstrap_removed"] is True
    assert calls == ["setup", "installed setup", "doctor", "dropin"]


def test_fresh_restore_preparation_stops_and_disables_unit(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from rcp_supervisor import runtime

    state = tmp_path / "state"
    state.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    (data / "old").write_text("retained")
    plan = tmp_path / "source.json"
    plan.write_text(json.dumps({"kind": "fresh_restore"}))
    monkeypatch.setattr(guest, "STATE", state)
    monkeypatch.setattr(guest, "PLAN", tmp_path / "plan.json")
    monkeypatch.setattr(runtime, "Paths", lambda: SimpleNamespace(data_dir=data))
    monkeypatch.setattr(guest.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=1, pw_gid=1))
    monkeypatch.setattr(guest.os, "chown", lambda *args: None)
    monkeypatch.setattr(
        guest, "write_json", lambda path, value, **kwargs: path.write_text(json.dumps(value))
    )
    commands = []
    monkeypatch.setattr(guest, "wait_health", lambda: commands.append("healthy"))
    monkeypatch.setattr(guest, "run", lambda argv: commands.append(argv))
    assert guest.prepare_case(plan) == {"status": "prepared"}
    assert commands == [
        "healthy",
        ["systemctl", "stop", "rcp.service"],
        ["systemctl", "disable", "rcp.service"],
    ]
    assert not list(data.iterdir())
    assert (state / "fresh-original-data/old").read_text() == "retained"


@pytest.mark.parametrize("kind", ["update", "restore", "fresh_restore", "invalid"])
def test_case_does_not_arm_faults_before_baseline_application_is_healthy(
    tmp_path, monkeypatch, kind
):
    plan = tmp_path / "source.json"
    plan.write_text(json.dumps({"kind": kind}))
    state = tmp_path / "state"
    state.mkdir()
    retained = state / "events.jsonl"
    retained.write_text("baseline startup still running\n")
    armed = tmp_path / "armed.json"
    monkeypatch.setattr(guest, "STATE", state)
    monkeypatch.setattr(guest, "PLAN", armed)

    def unavailable():
        raise RuntimeError("The guest application never returned healthy HTTP.")

    monkeypatch.setattr(guest, "wait_health", unavailable)
    with pytest.raises(RuntimeError, match="never returned healthy"):
        guest.prepare_case(plan)
    assert not armed.exists()
    assert not (state / "state.json").exists()
    assert retained.read_text() == "baseline startup still running\n"
