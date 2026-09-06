from __future__ import annotations

import hashlib
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path

import pytest
from rcp_supervisor import migration
from rcp_supervisor.errors import SupervisorError
from rcp_supervisor.runtime import Paths


class Crash(BaseException):
    pass


class Runtime:
    def __init__(self, root):
        self.paths = Paths(
            config=root / "etc/rcp/server.toml",
            supervisor=root / "etc/rcp/supervisor",
            current=root / "etc/rcp/current",
            data_dir=root / "home/rcp/rcp-server/data",
            releases_root=root / "home/rcp/rcp-server/releases",
            checkpoints_root=root / "home/rcp/rcp-server/update-checkpoints",
        )
        for path in (
            self.paths.supervisor,
            self.paths.data_dir,
            self.paths.releases_root,
            self.paths.checkpoints_root,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.paths.operations.mkdir()
        self.config = {
            "installation_id": "existing-installation",
            "backup": {"destination": str(root / "backups"), "age_recipient": "public-recipient"},
        }
        self.uid = os.geteuid()
        self.startup = False
        self.calls = []
        self.crash = None
        self.fail_probe = False
        self.deployment_locked = False
        self.legacy = self.paths.releases_root / ("a" * 40)
        self.legacy.mkdir()
        self.paths.current.symlink_to(self.legacy)
        (self.paths.data_dir / "database").write_bytes(b"original schema and rows")
        self.target = {
            "version": 1,
            "release_tag": "v0.3.2",
            "version_string": "0.3.2+build.412.gbbbbbbb",
            "build": 412,
            "commit": "b" * 40,
            "manifest_sha256": "c" * 64,
            "release_directory": str(self.paths.releases_root / "412"),
            "supervisor_version": "0.1.1",
        }
        Path(self.target["release_directory"]).mkdir()
        old = {
            "config": 'schema_version = 2\ninstallation_id = "existing-installation"\n',
            "unit": "[Service]\nUser=rcp\nExecStart=/usr/local/bin/rcp serve\n",
            "wrapper": "#!/bin/sh\nlegacy entry\n",
        }
        files = {}
        for key, path in migration._integration_paths(self).items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(old[key])
            path.chmod(0o755 if key == "wrapper" else 0o644)
            files[key] = {
                "text": old[key].replace("schema_version = 2", "schema_version = 3")
                if key == "config"
                else "new " + old[key],
                "mode": 0o755 if key == "wrapper" else 0o644,
                "gid": os.getegid(),
            }
        (self.paths.supervisor / "integration.json").write_text(
            json.dumps({"version": 1, "files": files})
        )

    def notify(self, phase):
        self.calls.append(phase)
        if phase == self.crash:
            self.crash = None
            raise Crash(phase)

    def metadata(self):
        return {"running_commit": self.paths.current.readlink().name, "app_version": "0.3.2"}

    def protected_backup(self, previous):
        assert not self.deployment_locked
        self.calls.append("protected_backup")

    @contextmanager
    def deployment_lock(self):
        assert not self.deployment_locked
        self.deployment_locked = True
        self.calls.append("deployment_lock")
        try:
            yield
        finally:
            self.deployment_locked = False

    def stop_service(self):
        assert not self.startup
        assert self.deployment_locked
        self.calls.append("stop")

    def start_service(self):
        assert not self.startup
        self.calls.append("start")

    def _systemctl(self, action):
        assert not self.startup
        self.calls.append(action)

    def filesystem(self, action, request):
        self.calls.append(action)
        if action == "retire-source-keys":
            migration.retire_source_keys(Path(request["credentials"]))
            return {"status": "retired"}
        path = Path(request["directory"])
        if action == "workspace":
            path.mkdir()
            return {"status": "ready"}
        path.mkdir()
        shutil.copyfile(self.paths.data_dir / "database", path / "payload")
        return {
            "directory": str(path),
            "sha256": "d" * 64,
            "boundary_sha256": request["boundary_sha256"],
        }

    def application(self, target, action, request):
        self.calls.append(action)
        assert (self.paths.data_dir / "database").read_bytes() == b"original schema and rows"
        if action == "offline-prepare":
            return {
                "roots": [],
                "boundary_sha256": "e" * 64,
                "proof_path": str(self.paths.checkpoints_root / "proof"),
                "proof_sha256": "f" * 64,
            }
        if action == "offline-protect":
            output = Path(request["output_dir"])
            output.mkdir(parents=True)
            result = {"version": 1, "status": "protected", "uncaptured_projects": 0}
            for label in ("archive", "receipt"):
                path = output / label
                path.write_bytes(b"protected capture")
                path.chmod(0o600)
                result[label + "_path"] = str(path)
                result[label + "_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            return result
        return {
            "proof_path": str(self.paths.checkpoints_root / "checked"),
            "proof_sha256": "f" * 64,
        }

    def probe(self, target, record, proof):
        self.calls.append("probe")
        (self.paths.data_dir / "database").write_bytes(b"migrated schema and rows")
        if self.fail_probe:
            raise SupervisorError("candidate verification failed")

    def restore_roots(self, checkpoint):
        assert self.deployment_locked
        self.calls.append("restore")
        shutil.copyfile(Path(checkpoint["directory"]) / "payload", self.paths.data_dir / "database")

    def select(self, target):
        self.calls.append("select")
        self.paths.selected.write_text(json.dumps(target))


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    instance = Runtime(tmp_path)

    def content(path):
        return {"text": path.read_text(), "mode": path.stat().st_mode & 0o777, "gid": os.getegid()}

    def write(path, item):
        path.write_text(item["text"])
        path.chmod(item["mode"])

    def pointer(runtime, target, *, allowed):
        assert os.readlink(runtime.paths.current) in allowed
        temporary = runtime.paths.current.with_name(".test-current")
        temporary.symlink_to(target)
        os.replace(temporary, runtime.paths.current)

    monkeypatch.setattr(migration, "_content", content)
    monkeypatch.setattr(migration, "_write_content", write)
    monkeypatch.setattr(migration, "_pointer", pointer)
    monkeypatch.setattr(migration, "_read_file", lambda path, **kwargs: path.read_bytes())
    monkeypatch.setattr(migration, "_read", lambda path: json.loads(path.read_bytes()))
    monkeypatch.setattr(
        migration, "write_root_json", lambda path, document: path.write_text(json.dumps(document))
    )
    monkeypatch.setattr(migration, "_reload", lambda runtime: None)
    monkeypatch.setattr(migration, "_legacy_health", lambda runtime, metadata: True)
    return instance


@pytest.mark.parametrize(
    "phase",
    [
        "prepared",
        "guarded",
        "stopped",
        "raw_checkpoint_ready",
        "checkpoint_ready",
        "probing",
        "candidate_chosen",
        "committed",
    ],
)
def test_adoption_crash_reentry_selects_only_the_safe_data_and_launch_pair(runtime, phase):
    runtime.crash = "adoption_" + phase
    with pytest.raises(Crash):
        migration.adopt(runtime, runtime.target)
    before_recovery = len(runtime.calls)
    runtime.startup = True
    migration.recover(runtime)
    assert not {"stop", "start"} & set(runtime.calls[before_recovery:])
    chosen = phase in {"candidate_chosen", "committed"}
    assert (runtime.paths.data_dir / "database").read_bytes() == (
        b"migrated schema and rows" if chosen else b"original schema and rows"
    )
    assert os.readlink(runtime.paths.current) == (
        runtime.target["release_directory"] if chosen else str(runtime.legacy)
    )
    assert runtime.paths.selected.exists() is chosen
    record = json.loads((runtime.paths.supervisor / "adoption.json").read_text())
    assert record["phase"] == ("committed" if chosen else "rolled_back")


def test_failed_candidate_restores_bytes_before_old_source_can_start(runtime):
    runtime.fail_probe = True
    with pytest.raises(SupervisorError, match="candidate verification"):
        migration.adopt(runtime, runtime.target)
    assert (
        runtime.calls.index("restore")
        < runtime.calls.index("adoption_previous_chosen")
        < runtime.calls.index("start")
    )
    assert (runtime.paths.data_dir / "database").read_bytes() == b"original schema and rows"
    assert not runtime.paths.selected.exists()


@pytest.mark.parametrize("phase", ["rollback_chosen", "previous_chosen", "rolled_back"])
def test_interrupted_adoption_rollback_resumes_without_restarting_from_startup(runtime, phase):
    runtime.fail_probe = True
    runtime.crash = "adoption_" + phase
    with pytest.raises(Crash):
        migration.adopt(runtime, runtime.target)
    restore_count = runtime.calls.count("restore")
    before_recovery = len(runtime.calls)
    runtime.startup = True
    assert migration.recover(runtime)["phase"] == "rolled_back"
    assert not {"stop", "start"} & set(runtime.calls[before_recovery:])
    assert (runtime.paths.data_dir / "database").read_bytes() == b"original schema and rows"
    assert os.readlink(runtime.paths.current) == str(runtime.legacy)
    assert not runtime.paths.selected.exists()
    if phase != "rollback_chosen":
        assert runtime.calls.count("restore") == restore_count
    assert migration._GUARD not in migration._integration_paths(runtime)["unit"].read_text()


def test_committed_adoption_defers_startup_authority_to_later_operations(runtime):
    migration.adopt(runtime, runtime.target)
    later = dict(runtime.target, build=413)
    runtime.paths.selected.write_text(json.dumps(later))
    runtime.paths.current.unlink()
    runtime.paths.current.symlink_to(runtime.paths.releases_root / "413")
    assert migration.startup_allowed(runtime) is False
    assert migration.recover(runtime) is None
    assert json.loads(runtime.paths.selected.read_text()) == later


def test_legacy_guard_replaces_short_startup_timeout_without_changing_other_sections():
    from rcp_supervisor.limits import STARTUP_RECOVERY_TIMEOUT_SECONDS

    text = "[Unit]\nDescription=Legacy\n[Service]\nUser=rcp\nTimeoutStartSec=30\n[Install]\nWantedBy=multi-user.target\n"
    guarded = migration._guarded_unit(text)
    assert guarded.count("TimeoutStartSec=") == 1
    assert f"TimeoutStartSec={STARTUP_RECOVERY_TIMEOUT_SECONDS}\n" in guarded
    assert migration._GUARD in guarded
    assert guarded.endswith("[Install]\nWantedBy=multi-user.target\n")


def test_complete_checkpoint_precedes_first_candidate_live_mutation(runtime):
    migration.adopt(runtime, runtime.target)
    assert (
        runtime.calls.index("snapshot")
        < runtime.calls.index("offline-prepare")
        < runtime.calls.index("checkpoint")
        < runtime.calls.index("probe")
    )
    assert (
        runtime.calls.index("adoption_candidate_chosen")
        < runtime.calls.index("select")
        < runtime.calls.index("start")
    )


def test_corrupt_adoption_journal_never_changes_data_or_launch(runtime):
    (runtime.paths.supervisor / "adoption.json").write_text(
        json.dumps({"phase": "candidate_chosen"})
    )
    with pytest.raises(SupervisorError):
        migration.recover(runtime)
    assert runtime.calls == ["deployment_lock"]
    assert (runtime.paths.data_dir / "database").read_bytes() == b"original schema and rows"


def test_known_legacy_partial_backup_requires_new_complete_encrypted_capture(runtime, monkeypatch):
    def partial(previous):
        runtime.calls.append("legacy_partial")
        raise SupervisorError("legacy backup was partial")

    monkeypatch.setattr(runtime, "protected_backup", partial)
    migration.adopt(runtime, runtime.target)
    record = json.loads((runtime.paths.supervisor / "adoption.json").read_text())
    assert record["legacy_backup"] == "unavailable"
    assert record["protected_backup"]["archive_sha256"]
    assert (
        runtime.calls.index("snapshot")
        < runtime.calls.index("offline-protect")
        < runtime.calls.index("probe")
    )


@pytest.mark.parametrize("failure", ["partial", "changed_archive"])
def test_incomplete_or_changed_offline_backup_prevents_candidate_live_mutation(
    runtime, monkeypatch, failure
):
    original = runtime.application

    def application(target, action, request):
        result = original(target, action, request)
        if action == "offline-protect":
            if failure == "partial":
                result["status"] = "partial"
            else:
                Path(result["archive_path"]).write_bytes(b"changed")
        return result

    monkeypatch.setattr(runtime, "application", application)
    with pytest.raises(SupervisorError):
        migration.adopt(runtime, runtime.target)
    assert "probe" not in runtime.calls
    assert (runtime.paths.data_dir / "database").read_bytes() == b"original schema and rows"
    assert os.readlink(runtime.paths.current) == str(runtime.legacy)


def test_success_retires_only_legacy_source_keys_after_commit(runtime):
    credentials = runtime.paths.data_dir.parent / "credentials"
    credentials.mkdir(mode=0o700)
    for name, mode in [
        ("source_ed25519", 0o600),
        ("source_ed25519.pub", 0o644),
        ("project-key", 0o600),
    ]:
        path = credentials / name
        path.write_text("fixture key")
        path.chmod(mode)
    migration.adopt(runtime, runtime.target)
    assert sorted(path.name for path in credentials.iterdir()) == ["project-key"]
    record = json.loads((runtime.paths.supervisor / "adoption.json").read_text())
    assert record["phase"] == "committed"


def test_repeated_source_key_retirement_syncs_already_absent_names(tmp_path, monkeypatch):
    credentials = tmp_path / "credentials"
    credentials.mkdir(mode=0o700)
    synced = []
    monkeypatch.setattr(migration, "_sync", synced.append)
    migration.retire_source_keys(credentials)
    assert synced == [credentials]


def test_deployment_lock_preparation_preserves_existing_inode(tmp_path):
    directory = tmp_path / "server"
    directory.mkdir(mode=0o700)
    migration.prepare_deployment_lock(directory)
    lock = directory / ".backup-run.lock"
    original = lock.stat()
    migration.prepare_deployment_lock(directory)
    assert lock.stat().st_ino == original.st_ino
    assert lock.stat().st_mode & 0o777 == 0o600
    assert (lock.stat().st_uid, lock.stat().st_gid) == (os.geteuid(), os.getegid())


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "permissions"])
def test_deployment_lock_preparation_rejects_unsafe_existing_files(tmp_path, unsafe):
    directory = tmp_path / "server"
    directory.mkdir(mode=0o700)
    lock = directory / ".backup-run.lock"
    outside = tmp_path / "outside"
    outside.write_bytes(b"preserve")
    outside.chmod(0o600)
    if unsafe == "symlink":
        lock.symlink_to(outside)
    elif unsafe == "hardlink":
        os.link(outside, lock)
    else:
        lock.write_bytes(b"preserve")
        lock.chmod(0o644)
    with pytest.raises((SupervisorError, OSError)):
        migration.prepare_deployment_lock(directory)
    assert outside.read_bytes() == b"preserve"


def test_install_retries_terminal_rolled_back_adoption(runtime, monkeypatch):
    from contextlib import nullcontext
    from io import StringIO
    from types import SimpleNamespace

    from rcp_supervisor import driver
    from rcp_supervisor.events import EventEmitter

    original_config = dict(runtime.config)
    runtime.fail_probe = True
    with pytest.raises(SupervisorError, match="candidate verification"):
        migration.adopt(runtime, runtime.target)
    journal = runtime.paths.supervisor / "adoption.json"
    previous = json.loads(journal.read_text())
    assert previous["phase"] == "rolled_back"
    runtime.fail_probe = False
    runtime.config = {**original_config, "schema_version": 2}
    monkeypatch.setattr(driver, "SystemRuntime", lambda *args, **kwargs: runtime)
    monkeypatch.setattr(driver, "_root_directory", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        driver, "store_for", lambda _: SimpleNamespace(locked=nullcontext, active=lambda: None)
    )
    monkeypatch.setattr(
        driver, "followed_release", lambda _: SimpleNamespace(directory=runtime.paths.releases_root)
    )
    monkeypatch.setattr(driver, "_require_supervisor", lambda _: None)
    monkeypatch.setattr(driver, "prepare_release", lambda *args: runtime.target)
    emitter = EventEmitter("server install", stream=StringIO())
    emitter.emit("running", "Install")
    assert driver.install(SimpleNamespace(team_name="Team"), emitter, paths=runtime.paths) == 0
    assert json.loads(journal.read_text())["phase"] == "committed"
    archived = journal.with_name("adoption-" + previous["operation_id"] + ".json")
    assert json.loads(archived.read_text()) == previous
