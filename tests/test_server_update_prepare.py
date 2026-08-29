from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.__main__ import build_parser
from rcp.server_ops.cli import (
    SERVER_CLI_EXIT_OPERATOR_ACTION,
    CallerIdentity,
    run_server_command,
)
from rcp.server_ops.layout import ServerLayout
from rcp.server_ops.models import ServerCommandRequest
from rcp.server_ops.update import (
    BuiltCandidateReceipt,
    CandidateBuild,
    LinuxUpdateMachine,
    UpdateInspection,
    UpdateRefused,
    UpdateTarget,
    built_candidate_receipt_path,
    prepare_update_command,
)
from rcp.server_runtime import web_build_identity

INSTALLATION_ID = "123e4567-e89b-42d3-a456-426614174000"
INSTANCE_ID = "123e4567-e89b-42d3-b456-426614174001"
BASE = "a" * 40
TARGET = "b" * 40
NEWER = "c" * 40
WEB_BUILD_ID = "sha256:" + ("d" * 64)
ROOT = CallerIdentity(uid=0, username="root", host="lab.example")


class _Paths:
    def __init__(self, layout: ServerLayout) -> None:
        self.layout = layout

    def model_dump(self) -> dict[str, str]:
        return self.layout.recorded_paths()


def _config(layout: ServerLayout, *, origin: str = "https://github.com/openai/rcp.git"):
    return SimpleNamespace(
        installation_id=INSTALLATION_ID,
        source=SimpleNamespace(
            origin=origin,
            branch="main",
            authentication="public",
            public_key_fingerprint=None,
        ),
        paths=_Paths(layout),
    )


def _inspection(
    layout: ServerLayout,
    *,
    managed: str = BASE,
    current: str = BASE,
    running: str = BASE,
    origin: str = "https://github.com/openai/rcp.git",
) -> UpdateInspection:
    return UpdateInspection(
        config=_config(layout, origin=origin),
        managed_head=managed,
        current_commit=current,
        running_commit=running,
        instance_id=INSTANCE_ID,
        process_pid=421,
    )


class FakeUpdateMachine:
    def __init__(
        self,
        layout: ServerLayout,
        *,
        target_commit: str = TARGET,
        fail_at: str | None = None,
        output: StringIO | None = None,
    ) -> None:
        self.layout = layout
        self.target_commit = target_commit
        self.fail_at = fail_at
        self.output = output
        self.observed = _inspection(layout)
        self.calls: list[str] = []

    @contextmanager
    def admission(self):
        self.calls.append("admission_enter")
        if self.output is not None:
            first = json.loads(self.output.getvalue().splitlines()[0])
            assert first["event"] == "plan"
        if self.fail_at == "admission":
            raise UpdateRefused("update admission fixture refused")
        try:
            yield
        finally:
            self.calls.append("admission_exit")

    def inspect(self) -> UpdateInspection:
        self.calls.append("inspect")
        if self.fail_at == "inspect":
            raise UpdateRefused("doctor fixture refused")
        return self.observed

    def status(self) -> UpdateInspection:
        self.calls.append("status")
        return self.observed

    def fetch_target(self, inspection: UpdateInspection) -> UpdateTarget:
        self.calls.append("fetch")
        if self.fail_at == "fetch":
            raise UpdateRefused("fetch fixture refused")
        return UpdateTarget(inspection=inspection, target_commit=self.target_commit)

    def fast_forward(self, target: UpdateTarget) -> None:
        self.calls.append("fast_forward")
        if self.fail_at == "fast_forward":
            raise UpdateRefused("fast-forward fixture refused")
        self.observed = replace(self.observed, managed_head=target.target_commit)

    def prepare_release(self, target: UpdateTarget) -> Path:
        self.calls.append("prepare_release")
        if self.fail_at == "prepare_release":
            raise UpdateRefused("release fixture refused")
        return self.layout.release_dir(target.target_commit)

    def build_candidate(self, target: UpdateTarget, release: Path) -> CandidateBuild:
        self.calls.append("build_candidate")
        if self.fail_at == "build_candidate":
            raise UpdateRefused("build fixture refused")
        return CandidateBuild(
            commit=target.target_commit,
            release_path=release,
            web_build_id=WEB_BUILD_ID,
            reused_receipt=False,
        )

    def finalize_candidate(
        self,
        target: UpdateTarget,
        build: CandidateBuild,
    ) -> BuiltCandidateReceipt:
        self.calls.append("finalize_candidate")
        if self.fail_at == "finalize_candidate":
            raise UpdateRefused("receipt fixture refused")
        return _receipt(self.layout, target=target.target_commit)


def _run_update(
    layout: ServerLayout,
    machine: FakeUpdateMachine,
    *,
    confirmed: str | None = None,
) -> tuple[int, list[dict]]:
    argv = ["server", "update", "--machine-readable"]
    if confirmed is not None:
        argv.extend(("--confirm-target", confirmed))
    args = build_parser().parse_args(argv)
    output = machine.output or StringIO()
    machine.output = output

    def handler(request: ServerCommandRequest, identity: CallerIdentity):
        return prepare_update_command(
            request,
            identity,
            machine=machine,
            resume_executable=layout.cli_wrapper,
        )

    exit_code = run_server_command(args, identity=ROOT, handler=handler, stream=output)
    return exit_code, [json.loads(line) for line in output.getvalue().splitlines()]


def _final_fields(events: list[dict]) -> dict[str, object]:
    return {item["name"]: item["value"] for item in events[-1]["step"].get("fields", [])}


def test_update_emits_plan_before_fetch_and_pauses_on_one_exact_target(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    output = StringIO()
    machine = FakeUpdateMachine(layout, output=output)

    exit_code, events = _run_update(layout, machine)

    assert exit_code == SERVER_CLI_EXIT_OPERATOR_ACTION
    assert machine.calls == ["admission_enter", "inspect", "fetch", "admission_exit"]
    assert events[0]["event"] == "plan"
    assert len(events[0]["steps"]) == 7
    paused = events[-1]["step"]
    assert paused["number"] == 3
    assert paused["state"] == "operator_action_needed"
    assert paused["resume_argv"] == [
        "sudo",
        str(layout.cli_wrapper),
        "server",
        "update",
        "--confirm-target",
        TARGET,
    ]
    assert _final_fields(events)["target_commit"] == TARGET


def test_default_machine_resolution_happens_only_after_the_plan_is_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = StringIO()

    def fail_machine_resolution():
        assert json.loads(output.getvalue().splitlines()[0])["event"] == "plan"
        raise UpdateRefused("fixture account lookup failed")

    monkeypatch.setattr(
        "rcp.server_ops.update.LinuxUpdateMachine",
        fail_machine_resolution,
    )
    args = build_parser().parse_args(("server", "update", "--machine-readable"))

    exit_code = run_server_command(args, identity=ROOT, stream=output)

    assert exit_code == 1
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert events[0]["event"] == "plan"
    assert events[-1]["step"]["number"] == 1
    assert events[-1]["step"]["state"] == "failed"


def test_update_refetches_and_refuses_a_stale_confirmation(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    machine = FakeUpdateMachine(layout, target_commit=NEWER)

    exit_code, events = _run_update(layout, machine, confirmed=TARGET)

    assert exit_code == 1
    assert events[-1]["step"]["number"] == 3
    assert events[-1]["step"]["state"] == "failed"
    assert _final_fields(events)["target_commit"] == NEWER
    assert "fast_forward" not in machine.calls
    assert "build_candidate" not in machine.calls


def test_confirmed_update_builds_candidate_without_switching_live_release(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    machine = FakeUpdateMachine(layout)

    exit_code, events = _run_update(layout, machine, confirmed=TARGET)

    assert exit_code == 0
    assert machine.calls == [
        "admission_enter",
        "inspect",
        "fetch",
        "fast_forward",
        "prepare_release",
        "build_candidate",
        "finalize_candidate",
        "admission_exit",
    ]
    assert all(event["step"]["state"] == "succeeded" for event in events[2::2])
    fields = _final_fields(events)
    assert fields == {
        "update_state": "candidate_built",
        "candidate_commit": TARGET,
        "current_commit": BASE,
        "running_commit": BASE,
        "release_path": str(layout.release_dir(TARGET)),
        "receipt_path": str(built_candidate_receipt_path(TARGET, layout)),
    }
    assert not layout.current_release.exists()


def test_update_skips_confirmation_and_mutation_when_origin_main_is_running(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    machine = FakeUpdateMachine(layout, target_commit=BASE)

    exit_code, events = _run_update(layout, machine)

    assert exit_code == 0
    assert machine.calls == ["admission_enter", "inspect", "fetch", "admission_exit"]
    assert _final_fields(events) == {
        "update_state": "already_current",
        "current_commit": BASE,
        "running_commit": BASE,
        "candidate_commit": "none",
    }


def test_already_current_update_still_refuses_an_explicit_stale_confirmation(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    machine = FakeUpdateMachine(layout, target_commit=BASE)

    exit_code, events = _run_update(layout, machine, confirmed=TARGET)

    assert exit_code == 1
    assert events[-1]["step"]["number"] == 3
    assert events[-1]["step"]["state"] == "failed"
    assert machine.calls == ["admission_enter", "inspect", "fetch", "admission_exit"]


def test_build_failure_reports_exact_post_fast_forward_and_unchanged_live_state(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    machine = FakeUpdateMachine(layout, fail_at="build_candidate")

    exit_code, events = _run_update(layout, machine, confirmed=TARGET)

    assert exit_code == 1
    assert events[-1]["step"]["number"] == 6
    assert events[-1]["step"]["state"] == "failed"
    assert _final_fields(events) == {
        "managed_main_head": TARGET,
        "candidate_commit": TARGET,
        "current_commit": BASE,
        "running_commit": BASE,
    }
    assert machine.calls[-3:] == ["build_candidate", "status", "admission_exit"]
    assert "finalize_candidate" not in machine.calls


def test_fetch_failure_reports_known_identities_without_inventing_a_candidate(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    machine = FakeUpdateMachine(layout, fail_at="fetch")

    exit_code, events = _run_update(layout, machine)

    assert exit_code == 1
    assert _final_fields(events) == {
        "managed_main_head": BASE,
        "current_commit": BASE,
        "running_commit": BASE,
        "candidate_commit": "unavailable",
    }
    assert machine.calls == ["admission_enter", "inspect", "fetch", "admission_exit"]


def test_update_build_command_order_and_environment_are_fixed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    calls: list[tuple[tuple[str, ...], Path | None, dict[str, str] | None]] = []

    def runner(argv, *, cwd, environment, timeout, capture_output):
        del timeout, capture_output
        calls.append((argv, cwd, environment))
        return subprocess.CompletedProcess(argv, 0, "", "")

    machine = LinuxUpdateMachine(
        layout,
        config_loader=lambda _path: _config(layout),
        doctor=SimpleNamespace(inspect=lambda: None),
        service_runner=runner,
        service_identity=(os.getuid(), os.getgid()),
        root_identity=(os.getuid(), os.getgid()),
    )
    target = UpdateTarget(inspection=_inspection(layout), target_commit=TARGET)
    release = layout.release_dir(TARGET)
    monkeypatch.setattr(machine, "_read_receipt_if_present", lambda _commit: None)
    monkeypatch.setattr(machine, "_validate_release_git", lambda _release, _commit: None)
    monkeypatch.setattr(
        machine,
        "_validate_built_release",
        lambda _release, _commit: WEB_BUILD_ID,
    )

    built = machine.build_candidate(target, release)

    assert built == CandidateBuild(
        commit=TARGET,
        release_path=release,
        web_build_id=WEB_BUILD_ID,
        reused_receipt=False,
    )
    assert [call[0] for call in calls] == [
        ("npm", "--prefix", "web", "ci"),
        ("npm", "--prefix", "web", "run", "build"),
        ("uv", "sync", "--frozen"),
    ]
    assert all(call[1] == release for call in calls)
    assert calls[0][2] is None
    assert calls[1][2] is None
    assert calls[2][2] == {"UV_MANAGED_PYTHON": "1", "UV_PYTHON": "3.12"}
    assert all("systemctl" not in call[0] for call in calls)
    assert not layout.current_release.exists()


def test_failure_status_can_report_identities_even_when_doctor_blocks_update(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    config = _config(layout)
    report = SimpleNamespace(
        problems=("managed source checkout has local changes",),
        managed_main_head=TARGET,
        current_commit=BASE,
        running_commit=BASE,
        instance_id=INSTANCE_ID,
        process_pid=421,
        release_state="candidate_pending",
        installation_id=INSTALLATION_ID,
        configured_origin=config.source.origin,
        configured_branch="main",
    )
    machine = LinuxUpdateMachine(
        layout,
        config_loader=lambda _path: config,
        doctor=SimpleNamespace(inspect=lambda: report),
        service_runner=lambda *args, **kwargs: pytest.fail("no subprocess expected"),
        service_identity=(os.getuid(), os.getgid()),
        root_identity=(os.getuid(), os.getgid()),
    )

    assert machine.status().managed_head == TARGET
    with pytest.raises(UpdateRefused, match="Server doctor blocks update"):
        machine.inspect()


def test_real_local_origin_fetch_fast_forward_and_detached_release(tmp_path: Path) -> None:
    remote = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    layout = _layout(tmp_path / "installed")
    _run(("git", "init", "--bare", "-q", str(remote)))
    _run(("git", "init", "-q", "-b", "main", str(seed)))
    _run(("git", "-C", str(seed), "config", "user.name", "RCP Test"))
    _run(("git", "-C", str(seed), "config", "user.email", "rcp@example.test"))
    (seed / "README.md").write_text("one\n", encoding="utf-8")
    (seed / ".gitignore").write_text(".venv/\nweb/dist/\n", encoding="utf-8")
    _run(("git", "-C", str(seed), "add", "README.md", ".gitignore"))
    _run(("git", "-C", str(seed), "commit", "-q", "-m", "one"))
    base = _git_text(seed, "rev-parse", "HEAD")
    _run(("git", "-C", str(seed), "remote", "add", "origin", str(remote)))
    _run(("git", "-C", str(seed), "push", "-q", "-u", "origin", "main"))
    layout.source_checkout.parent.mkdir(parents=True)
    _run(
        (
            "git",
            "clone",
            "-q",
            "--branch",
            "main",
            str(remote),
            str(layout.source_checkout),
        )
    )
    (seed / "README.md").write_text("two\n", encoding="utf-8")
    _run(("git", "-C", str(seed), "commit", "-q", "-am", "two"))
    target_commit = _git_text(seed, "rev-parse", "HEAD")
    _run(("git", "-C", str(seed), "push", "-q", "origin", "main"))
    _prepare_owned_roots(layout)
    config = _config(layout, origin=str(remote))
    report = SimpleNamespace(
        problems=(),
        managed_main_head=base,
        current_commit=base,
        running_commit=base,
        instance_id=INSTANCE_ID,
        process_pid=421,
        release_state="aligned",
        installation_id=INSTALLATION_ID,
        configured_origin=str(remote),
        configured_branch="main",
    )
    calls: list[tuple[tuple[str, ...], dict[str, str] | None]] = []

    def runner(argv, *, cwd, environment, timeout, capture_output):
        calls.append((argv, environment))
        process_environment = {
            "HOME": str(layout.service_home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C",
        }
        if environment:
            process_environment.update(environment)
        return subprocess.run(
            argv,
            cwd=cwd,
            env=process_environment,
            timeout=timeout,
            capture_output=capture_output,
            text=True,
            check=False,
        )

    machine = LinuxUpdateMachine(
        layout,
        config_loader=lambda _path: config,
        doctor=SimpleNamespace(inspect=lambda: report),
        service_runner=runner,
        service_identity=(os.getuid(), os.getgid()),
        root_identity=(os.getuid(), os.getgid()),
    )

    inspection = machine.inspect()
    fetched = machine.fetch_target(inspection)

    assert fetched.target_commit == target_commit
    assert _git_text(layout.source_checkout, "rev-parse", "HEAD") == base
    fetch_environment = next(environment for argv, environment in calls if "fetch" in argv)
    assert fetch_environment is not None
    assert fetch_environment["GIT_ASKPASS"] == "/bin/false"
    assert fetch_environment["GIT_CONFIG_VALUE_0"] == ""
    assert fetch_environment["GIT_CONFIG_VALUE_2"] == "/dev/null"
    assert fetch_environment["GIT_CONFIG_KEY_2"] == "core.hooksPath"

    machine.fast_forward(fetched)
    release = machine.prepare_release(fetched)

    assert _git_text(layout.source_checkout, "rev-parse", "HEAD") == target_commit
    assert _git_text(layout.source_checkout, "symbolic-ref", "--short", "HEAD") == "main"
    assert release == layout.release_dir(target_commit)
    assert _git_text(release, "rev-parse", "HEAD") == target_commit
    assert (
        subprocess.run(
            ("git", "-C", str(release), "symbolic-ref", "--quiet", "HEAD"),
            check=False,
            capture_output=True,
            text=True,
        ).returncode
        == 1
    )
    assert _git_text(release, "status", "--porcelain", "--untracked-files=all") == ""
    assert not layout.current_release.exists()

    executable = release / ".venv" / "bin" / "rcp"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    (executable.parent / "python").symlink_to(sys.executable)
    web_index = release / "web" / "dist" / "index.html"
    web_index.parent.mkdir(parents=True)
    web_index.write_text("<!doctype html><title>RCP</title>\n", encoding="utf-8")

    assert machine._validate_built_release(release, target_commit) == web_build_identity(
        web_index.parent
    )


def test_candidate_release_must_be_a_detached_managed_worktree(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    layout.source_checkout.parent.mkdir(parents=True)
    _run(("git", "init", "-q", "-b", "main", str(layout.source_checkout)))
    _run(
        (
            "git",
            "-C",
            str(layout.source_checkout),
            "config",
            "user.name",
            "RCP Test",
        )
    )
    _run(
        (
            "git",
            "-C",
            str(layout.source_checkout),
            "config",
            "user.email",
            "rcp@example.test",
        )
    )
    (layout.source_checkout / "README.md").write_text("one\n", encoding="utf-8")
    _run(("git", "-C", str(layout.source_checkout), "add", "README.md"))
    _run(("git", "-C", str(layout.source_checkout), "commit", "-q", "-m", "one"))
    commit = _git_text(layout.source_checkout, "rev-parse", "HEAD")
    _prepare_owned_roots(layout)
    release = layout.release_dir(commit)
    _run(
        (
            "git",
            "-C",
            str(layout.source_checkout),
            "worktree",
            "add",
            "-q",
            "-b",
            "candidate",
            str(release),
            commit,
        )
    )
    machine = LinuxUpdateMachine(
        layout,
        config_loader=lambda _path: _config(layout),
        doctor=SimpleNamespace(inspect=lambda: None),
        service_runner=lambda argv, **kwargs: subprocess.run(
            argv,
            cwd=kwargs.get("cwd"),
            env=kwargs.get("environment"),
            timeout=kwargs["timeout"],
            capture_output=kwargs["capture_output"],
            text=True,
            check=False,
        ),
        service_identity=(os.getuid(), os.getgid()),
        root_identity=(os.getuid(), os.getgid()),
    )

    with pytest.raises(UpdateRefused, match="attached to a branch"):
        machine._validate_release_git(release, commit)


def test_update_lock_and_active_maintenance_fail_closed(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _prepare_owned_roots(layout)
    kwargs = {
        "config_loader": lambda _path: _config(layout),
        "doctor": SimpleNamespace(inspect=lambda: None),
        "service_runner": lambda *args, **kwargs: pytest.fail("no subprocess expected"),
        "service_identity": (os.getuid(), os.getgid()),
        "root_identity": (os.getuid(), os.getgid()),
    }
    first = LinuxUpdateMachine(layout, **kwargs)
    second = LinuxUpdateMachine(layout, **kwargs)

    with (
        first.admission(),
        pytest.raises(UpdateRefused, match="Another server update"),
        second.admission(),
    ):
        pytest.fail("concurrent admission should not enter")

    restore_marker = layout.restore_operations_root / "restore.json"
    restore_marker.write_text("{}\n", encoding="utf-8")
    with pytest.raises(UpdateRefused, match="unfinished restore"), first.admission():
        pytest.fail("restore maintenance should block update")
    restore_marker.unlink()

    unknown = layout.update_checkpoints_root / "partial-build"
    unknown.write_text("incomplete\n", encoding="utf-8")
    with (
        pytest.raises(
            UpdateRefused,
            match="Unfinished update maintenance",
        ),
        first.admission(),
    ):
        pytest.fail("unknown update maintenance should block update")


def test_candidate_receipt_is_private_validated_and_never_overwritten(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _prepare_owned_roots(layout)
    machine = LinuxUpdateMachine(
        layout,
        config_loader=lambda _path: _config(layout),
        doctor=SimpleNamespace(inspect=lambda: None),
        service_runner=lambda *args, **kwargs: pytest.fail("no subprocess expected"),
        service_identity=(os.getuid(), os.getgid()),
        root_identity=(os.getuid(), os.getgid()),
    )
    receipt = _receipt(layout)

    published = machine._publish_receipt(receipt)
    path = built_candidate_receipt_path(TARGET, layout)

    assert published == receipt
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    later = receipt.model_copy(update={"prepared_at": receipt.prepared_at + timedelta(hours=1)})
    assert machine._publish_receipt(later) == receipt
    assert machine._read_receipt(path) == receipt

    os.chmod(path, 0o644)
    with pytest.raises(UpdateRefused, match="unsafe or invalid"):
        machine._read_receipt(path)


def test_candidate_receipt_cannot_name_different_web_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    machine = LinuxUpdateMachine(
        layout,
        config_loader=lambda _path: _config(layout),
        doctor=SimpleNamespace(inspect=lambda: None),
        service_runner=lambda *args, **kwargs: pytest.fail("no subprocess expected"),
        service_identity=(os.getuid(), os.getgid()),
        root_identity=(os.getuid(), os.getgid()),
    )
    base_inspection = _inspection(layout)
    target = UpdateTarget(inspection=base_inspection, target_commit=TARGET)
    build = CandidateBuild(
        commit=TARGET,
        release_path=layout.release_dir(TARGET),
        web_build_id=WEB_BUILD_ID,
        reused_receipt=False,
    )
    mismatched = _receipt(layout).model_copy(update={"web_build_id": "sha256:" + ("e" * 64)})
    monkeypatch.setattr(
        machine,
        "_validate_built_release",
        lambda _release, _commit: WEB_BUILD_ID,
    )
    monkeypatch.setattr(machine, "inspect", lambda: _inspection(layout, managed=TARGET))
    monkeypatch.setattr(machine, "_publish_receipt", lambda _receipt: mismatched)

    with pytest.raises(UpdateRefused, match="different Web bytes"):
        machine.finalize_candidate(target, build)


def _receipt(layout: ServerLayout, *, target: str = TARGET) -> BuiltCandidateReceipt:
    return BuiltCandidateReceipt(
        installation_id=INSTALLATION_ID,
        source_origin="https://github.com/openai/rcp.git",
        base_current_commit=BASE,
        base_running_commit=BASE,
        base_instance_id=INSTANCE_ID,
        base_process_pid=421,
        candidate_commit=target,
        release_path=str(layout.release_dir(target)),
        receipt_path=str(built_candidate_receipt_path(target, layout)),
        web_build_id=WEB_BUILD_ID,
        prepared_at=datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
    )


def _layout(root: Path) -> ServerLayout:
    home = root / "home" / "rcp"
    server = home / "rcp-server"
    return ServerLayout(
        service_account="rcp",
        service_home=home,
        server_root=server,
        source_checkout=server / "source",
        releases_root=server / "releases",
        data_dir=server / "data",
        projects_root=server / "projects",
        credentials_root=server / "credentials",
        update_checkpoints_root=server / "update-checkpoints",
        restore_operations_root=server / "restore-operations",
        codex_state_root=home / ".codex",
        claude_state_root=home / ".claude",
        ssh_state_root=home / ".ssh",
        config_path=root / "etc" / "rcp" / "server.toml",
        current_release=root / "etc" / "rcp" / "current",
        runtime_dir=root / "run" / "rcp",
        control_socket=root / "run" / "rcp" / "control.sock",
        cli_wrapper=root / "usr" / "local" / "bin" / "rcp",
        systemd_unit=root / "etc" / "systemd" / "system" / "rcp.service",
        service_unit_name="rcp.service",
    )


def _prepare_owned_roots(layout: ServerLayout) -> None:
    for path in (
        layout.service_home,
        layout.server_root,
        layout.releases_root,
        layout.update_checkpoints_root,
        layout.restore_operations_root,
        layout.config_path.parent,
    ):
        path.mkdir(parents=True, exist_ok=True)
    os.chmod(layout.update_checkpoints_root, 0o700)
    os.chmod(layout.restore_operations_root, 0o700)
    os.chmod(layout.releases_root, 0o700)
    os.chmod(layout.config_path.parent, 0o750)


def _run(argv: tuple[str, ...]) -> None:
    subprocess.run(argv, check=True, capture_output=True, text=True)


def _git_text(root: Path, *argv: str) -> str:
    return subprocess.run(
        ("git", "-C", str(root), *argv),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
