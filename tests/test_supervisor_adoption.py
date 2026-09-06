from __future__ import annotations

import io
import json
import subprocess
import tarfile
from types import SimpleNamespace

import pytest

from tests import supervisor_adoption_build as build
from tests import supervisor_adoption_guest as adoption


def test_source_bundle_preserves_requested_history_without_changing_the_checkout(tmp_path):
    source = tmp_path / "source"
    source.mkdir()

    def git(*arguments):
        return subprocess.run(
            ["git", "-C", str(source), *arguments],
            check=True,
            text=True,
            capture_output=True,
            timeout=30,
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Adoption fixture")
    git("config", "user.email", "adoption@example.invalid")
    (source / "version").write_text("historical\n")
    git("add", "version")
    git("commit", "-qm", "Historical merged source")
    previous = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    (source / "version").write_text("new current main\n")
    git("commit", "-qam", "Later main")
    current = git("rev-parse", "HEAD")
    bundle = tmp_path / "historical.bundle"
    assert build.source_bundle(str(source), previous, bundle) == tree
    target = tmp_path / "guest-checkout"
    subprocess.run(
        ["git", "clone", "--branch", "main", str(bundle), str(target)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    assert (target / "version").read_text() == "historical\n"
    assert git("rev-parse", "HEAD") == current
    assert not git("status", "--porcelain")
    assert (
        subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        == previous
    )


def test_node_package_contains_only_required_runtime_and_npm(tmp_path, monkeypatch):
    runtime = tmp_path / "node-runtime"
    (runtime / "bin").mkdir(parents=True)
    node = runtime / "bin/node"
    node.write_bytes(b"\x7fELF" + b"\0" * 14 + b"\x3e\x00")
    npm = runtime / "lib/node_modules/npm"
    (npm / "bin").mkdir(parents=True)
    (npm / "bin/npm-cli.js").write_text("// npm fixture\n")
    (runtime / "unrelated-runner-cache").write_text("must never be copied")
    monkeypatch.setattr(
        build,
        "run",
        lambda argv: SimpleNamespace(stdout="v24.1.0\n" if len(argv) == 2 else "11.0.0\n"),
    )
    output = tmp_path / "runtime.tar.gz"
    receipt = build.node_runtime(node, output)
    assert receipt["node_version"] == "v24.1.0" and receipt["npm_version"] == "11.0.0"
    with tarfile.open(output) as archive:
        names = archive.getnames()
        assert "bin/node" in names and "lib/node_modules/npm/bin/npm-cli.js" in names
        assert not any("cache" in name for name in names)
        assert archive.getmember("bin/npm").linkname == "../lib/node_modules/npm/bin/npm-cli.js"
    outside = tmp_path / "outside"
    outside.write_text("private")
    (npm / "unsafe").symlink_to(outside)
    with pytest.raises(ValueError, match="outside"):
        build.node_runtime(node, tmp_path / "unsafe.tar.gz")


def payload(tmp_path, monkeypatch):
    monkeypatch.setattr(adoption, "PAYLOAD", tmp_path)
    files = {}
    for name in ("historical-source.bundle", "node-runtime.tar.gz"):
        (tmp_path / name).write_bytes(b"fixture")
        files[name] = adoption.sha256(tmp_path / name)
    receipt = {
        "source_commit": adoption.SOURCE_COMMIT,
        "source_origin": adoption.SOURCE_ORIGIN,
        "source_tree": "a" * 40,
        "node_version": "v24.1.0",
        "npm_version": "11.0.0",
        "files": files,
    }
    (tmp_path / "package-receipt.json").write_text(json.dumps(receipt))
    return receipt


def test_payload_requires_exact_historical_identity_and_bytes(tmp_path, monkeypatch):
    receipt = payload(tmp_path, monkeypatch)
    assert adoption.verify_payload() == receipt
    (tmp_path / "historical-source.bundle").write_text("changed")
    with pytest.raises(ValueError, match="SHA-256"):
        adoption.verify_payload()
    receipt = payload(tmp_path, monkeypatch)
    receipt["source_commit"] = "b" * 40
    (tmp_path / "package-receipt.json").write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="historical baseline"):
        adoption.verify_payload()


@pytest.mark.parametrize(
    "bad_name,link", [("../../etc/rcp", None), ("bin/unrelated", None), ("bin/npm", "/etc/passwd")]
)
def test_runtime_archive_refuses_escape_or_unrelated_files_before_extraction(
    tmp_path, monkeypatch, bad_name, link
):
    monkeypatch.setattr(adoption, "PAYLOAD", tmp_path)
    with tarfile.open(tmp_path / "node-runtime.tar.gz", "w:gz") as archive:
        member = tarfile.TarInfo(bad_name)
        if link is not None:
            member.type = tarfile.SYMTYPE
            member.linkname = link
            archive.addfile(member)
        else:
            member.size = 1
            archive.addfile(member, io.BytesIO(b"x"))
    monkeypatch.setattr(
        adoption.guest, "run", lambda *_args, **_kwargs: pytest.fail("unsafe extraction")
    )
    with pytest.raises(ValueError, match="archive member|escapes"):
        adoption.install_node()


def test_bootstrap_checks_disposable_marker_before_any_host_mutation(monkeypatch):
    def refuse():
        raise RuntimeError("not a disposable guest")

    monkeypatch.setattr(adoption.guest, "require_guest", refuse)
    monkeypatch.setattr(
        adoption.guest, "run", lambda *_args, **_kwargs: pytest.fail("host mutation")
    )
    with pytest.raises(RuntimeError, match="disposable"):
        adoption.bootstrap()


def test_failed_bootstrap_retains_private_events_and_exports_only_failed_message(
    tmp_path, monkeypatch, capsys
):
    import sys

    from rcp_supervisor import releases

    from rcp import __main__ as app_cli
    from rcp.server_ops import install

    secret = "private-operator-field-must-not-be-exported"
    message = "The selected supervisor runtime could not be prepared."
    failed = {
        "event": "step",
        "step": {
            "state": "failed",
            "phase": "bootstrap",
            "message": message,
            "fields": [{"name": "private", "value": secret}],
            "actions": [{"argv": [secret]}],
        },
    }

    def fail():
        print(json.dumps(failed))
        print("truncated non-JSON terminal output " + secret)
        raise SystemExit(1)

    monkeypatch.setattr(adoption.guest, "STATE", tmp_path)
    monkeypatch.setattr(app_cli, "main", fail)
    # paired_bootstrap installs observation hooks in these process-local owners.
    monkeypatch.setattr(install, "LinuxInstallMachine", install.LinuxInstallMachine)
    monkeypatch.setattr(releases, "fetch_release", releases.fetch_release)
    original_argv = sys.argv
    with pytest.raises(RuntimeError, match=message) as failure:
        adoption.paired_bootstrap()
    assert sys.argv is original_argv
    events = tmp_path / "bootstrap-events.json"
    assert json.loads(events.read_text()) == {"events": [failed]}
    assert events.stat().st_mode & 0o777 == 0o600
    diagnostic = adoption.diagnostics()
    assert diagnostic == {
        "historical_install_completed": False,
        "bootstrap_failure": {"exit_code": 1, "phase": "bootstrap", "message": message},
    }
    public = json.dumps(diagnostic) + str(failure.value) + capsys.readouterr().out
    assert secret not in public


def test_partial_diagnostics_export_only_completed_historical_receipt_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(adoption.guest, "STATE", tmp_path)
    historical = {
        "source_commit": adoption.SOURCE_COMMIT,
        "source_version": "0.3.4",
        "installation_id": "synthetic-installation",
        "space_id": "synthetic-space",
        "service_uid": 1001,
        "service_pid": 123,
    }
    (tmp_path / "historical.json").write_text(json.dumps({**historical, "token": "private-token"}))
    assert adoption.diagnostics() == {
        "historical_install_completed": True,
        "historical_install": historical,
    }


def test_missing_failed_step_message_never_exports_raw_exit_text_or_event_fields():
    diagnostic = adoption.bootstrap_failure(
        [{"step": {"state": "failed", "fields": ["private-field"]}}],
        "private-exit-value",
    )
    assert diagnostic["exit_code"] == 1
    assert diagnostic["message"].startswith("No failed step message was emitted")
    assert "private-" not in json.dumps(diagnostic)


@pytest.mark.parametrize("failed", [True, False])
def test_doctor_diagnostics_retain_real_cli_report_and_preserve_failure(
    tmp_path, monkeypatch, failed
):
    from tests.test_server_doctor import _report, _run_doctor

    problem = "configured backup timer is not both active and enabled"
    code, output, _calls = _run_doctor(
        _report(problems=(problem,) if failed else ()), machine_readable=True
    )
    events = [json.loads(line) for line in output.splitlines()]
    secret = "private-doctor-field-must-not-be-exported"
    events[-1]["step"]["fields"].append({"name": "last_backup_failure", "value": secret})
    output = "\n".join(json.dumps(event) for event in events) + "\n" + secret

    def doctor(argv):
        assert argv == ["/usr/local/bin/rcp", "server", "doctor", "--machine-readable"]
        if failed:
            raise subprocess.CalledProcessError(code, argv, output=output)
        return subprocess.CompletedProcess(argv, code, stdout=output, stderr="")

    monkeypatch.setattr(adoption.guest, "STATE", tmp_path)
    monkeypatch.setattr(adoption.guest, "service", doctor)
    if failed:
        with pytest.raises(subprocess.CalledProcessError):
            adoption.verify_doctor()
    else:
        adoption.verify_doctor()
    receipt = adoption.diagnostics()["doctor"]
    assert receipt == {
        "exit_code": code,
        "state": "failed" if failed else "succeeded",
        "overall_state": "problems" if failed else "healthy",
        "backup_status": "not_configured",
        "backup_timer_active_state": "not_configured",
        "backup_timer_unit_file_state": "not_configured",
        "problems": problem if failed else "none",
    }
    assert secret not in json.dumps(adoption.diagnostics())
    assert (tmp_path / "doctor.json").stat().st_mode & 0o777 == 0o600


def test_doctor_diagnostics_refuse_unrelated_malformed_and_oversized_output():
    unavailable = {"exit_code": 1, "state": "unavailable"}
    for output in ("private non-JSON output", "[]", "x" * (128 * 1024 + 1)):
        assert adoption.doctor_diagnostic(output, 1) == unavailable
    event = {
        "version": 1,
        "event": "step",
        "command": "server doctor",
        "step": {
            "phase": "server_doctor",
            "state": "failed",
            "fields": [
                {"name": "problems", "value": "x" * 2001},
                {"name": "overall_state", "value": "problems\nprivate-terminal-output"},
                {"name": ["unhashable"], "value": "private-value"},
            ],
        },
    }
    assert adoption.doctor_diagnostic(json.dumps(event), 1) == {**unavailable, "state": "failed"}
    event["step"]["state"] = []
    assert adoption.doctor_diagnostic(json.dumps(event), 1) == unavailable
    event["command"] = "server backup run"
    assert adoption.doctor_diagnostic(json.dumps(event), 1) == unavailable


@pytest.fixture
def adopted_journal(tmp_path, monkeypatch):
    from rcp_supervisor import runtime
    from rcp_supervisor.launch import validate_selected_receipt

    paths = runtime.Paths(
        supervisor=tmp_path / "supervisor",
        releases_root=tmp_path / "releases",
        checkpoints_root=tmp_path / "checkpoints",
    )
    paths.supervisor.mkdir()
    selected = {
        "version": 1,
        "build": 900001,
        "release_tag": "v0.3.5",
        "version_string": "0.3.5+build.900001.g1111111",
        "commit": "1" * 40,
        "manifest_sha256": "a" * 64,
        "release_directory": str(paths.releases_root / "900001"),
        "supervisor_version": "0.1.3",
    }
    operation_id = "3b051624-4ffc-47bb-85bf-5dc1a4968e7d"
    integration = {
        key: {"text": "private integration text", "mode": 0o644, "gid": 0}
        for key in ("config", "unit", "wrapper")
    }
    record = {
        "version": 1,
        "operation_id": operation_id,
        "phase": "committed",
        "nonce": "b" * 64,
        "previous": {
            "commit": adoption.SOURCE_COMMIT,
            "release_directory": str(paths.releases_root / adoption.SOURCE_COMMIT),
            "version_string": "0.3.4",
        },
        "target": selected,
        "old": integration,
        "integration": integration,
        "target_proof": {"path": "/private/proof", "sha256": "c" * 64},
        "legacy_backup": "unavailable",
        "protected_backup": {
            "receipt_path": "/private/receipt",
            "receipt_sha256": "d" * 64,
            "archive_path": "/private/archive",
            "archive_sha256": "e" * 64,
        },
    }
    for field, name in (("raw_checkpoint", "raw-checkpoint"), ("checkpoint", "checkpoint")):
        record[field] = {
            "directory": str(paths.checkpoints_root / operation_id / name),
            "sha256": "f" * 64,
            "boundary_sha256": "b" * 64,
        }
    instance = SimpleNamespace(
        paths=paths,
        selected_release=lambda: validate_selected_receipt(
            selected, releases_root=paths.releases_root
        ),
    )
    monkeypatch.setattr(runtime, "SystemRuntime", lambda: instance)
    monkeypatch.setattr(adoption.guest, "release_receipt", lambda _path: selected)
    monkeypatch.setattr(adoption.guest, "STATE", tmp_path)
    adoption.guest.write_json(paths.supervisor / "adoption.json", record)
    return paths.supervisor / "adoption.json", record


def test_committed_selection_is_retained_without_exporting_private_journal(adopted_journal):
    path, record = adopted_journal
    adoption.record_adopted_selection()
    receipt_path = path.parent.parent / "adopted-selection.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["private_field"] = "private-selection-token"
    receipt["selected_release"]["private_field"] = "private-selection-token"
    adoption.guest.write_json(receipt_path, receipt)
    assert adoption.diagnostics() == {
        "historical_install_completed": False,
        "adopted_selection": {
            "phase": "committed",
            "operation_id": record["operation_id"],
            "source_commit": adoption.SOURCE_COMMIT,
            "selected_release": record["target"],
        },
    }
    public = json.dumps(adoption.diagnostics())
    assert "private integration text" not in public
    assert "/private/proof" not in public and "/private/archive" not in public
    assert "private-selection-token" not in public


@pytest.mark.parametrize(
    "defect", ["uncommitted", "wrong_source", "malformed_checkpoint", "wrong_target"]
)
def test_unproved_adoption_never_publishes_completed_selection(adopted_journal, defect):
    from rcp_supervisor.errors import SupervisorError

    path, record = adopted_journal
    if defect == "uncommitted":
        record["phase"] = "probing"
    elif defect == "wrong_source":
        record["previous"]["commit"] = "9" * 40
        record["previous"]["release_directory"] = str(path.parent.parent / "releases" / ("9" * 40))
    elif defect == "malformed_checkpoint":
        record["checkpoint"]["sha256"] = "not-a-digest"
    else:
        record["target"] = {**record["target"], "manifest_sha256": "9" * 64}
    adoption.guest.write_json(path, record)
    with pytest.raises((AssertionError, SupervisorError)):
        adoption.record_adopted_selection()
    assert "adopted_selection" not in adoption.diagnostics()
