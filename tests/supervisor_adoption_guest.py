"""Genuine historical-source adoption inside the existing marked QEMU guest.

``bootstrap`` prepares and starts the historical source installation, then runs
its current paired-wheel bootstrap and adoption. ``verify`` checks the result
again after the external controller reboots the guest. Only fixture release
provenance and observation hooks differ from the shipped entry points.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import pwd
import shutil
import sys
import tarfile
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path, PurePosixPath

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import supervisor_reboot_guest as guest

SOURCE_COMMIT = "203ad6aebb6a07eaea4b5ddc89cd6e5a324c0a10"
SOURCE_ORIGIN = "https://github.com/Zhi0467/RCP.git"
MAX_RUNTIME_BYTES = 512 * 1024 * 1024
PAYLOAD = guest.ROOT / "adoption"
SCRIPT = guest.ROOT / "tests/supervisor_adoption_guest.py"
BOOTSTRAP = guest.ROOT / "adoption-bootstrap/bin/python"
HISTORICAL = guest.ROOT / "historical-source"
DATA = Path("/home/rcp/rcp-server/data")
RELEASE = Path("/home/rcp/rcp-server/releases") / SOURCE_COMMIT
INTEGRATION = {
    "config": Path("/etc/rcp/server.toml"),
    "wrapper": Path("/usr/local/bin/rcp"),
    "unit": Path("/etc/systemd/system/rcp.service"),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def verify_payload() -> dict:
    receipt = guest.read_json(PAYLOAD / "package-receipt.json")
    if (
        receipt["source_commit"] != SOURCE_COMMIT
        or receipt["source_origin"] != SOURCE_ORIGIN
        or not receipt["node_version"].startswith("v24.")
        or set(receipt["files"]) != {"historical-source.bundle", "node-runtime.tar.gz"}
    ):
        raise ValueError("The adoption payload does not identify the exact historical baseline.")
    for name, digest in receipt["files"].items():
        if sha256(PAYLOAD / name) != digest:
            raise ValueError("The historical adoption payload failed its SHA-256 check.")
    return receipt


def install_node() -> None:
    """Accept only the bounded Node/npm subset emitted by the runner packager."""
    with tarfile.open(PAYLOAD / "node-runtime.tar.gz", "r:gz") as archive:
        total = 0
        names = set()
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or member.name in names
                or not (
                    member.name in {"bin/node", "bin/npm"}
                    or path.is_relative_to("lib/node_modules/npm")
                )
                or not (member.isfile() or member.isdir() or member.issym())
            ):
                raise ValueError("Unexpected historical Node/npm archive member.")
            names.add(member.name)
            total += member.size
            if member.issym():
                link = Path("/usr/local") / member.name
                target = (link.parent / member.linkname).resolve()
                if not target.is_relative_to("/usr/local/lib/node_modules/npm"):
                    raise ValueError("Historical npm link escapes its package.")
        if not {"bin/node", "bin/npm"} <= names or total > MAX_RUNTIME_BYTES:
            raise ValueError("Historical Node/npm archive is incomplete or exceeds its bound.")
    guest.run(
        [
            "tar",
            "--extract",
            "--gzip",
            "--file",
            str(PAYLOAD / "node-runtime.tar.gz"),
            "--directory",
            "/usr/local",
            "--no-same-owner",
            "--no-same-permissions",
        ],
        timeout=120,
    )


def source_identity(path: Path) -> dict:
    def git(*argv: str) -> str:
        return guest.run(
            ["git", "-c", f"safe.directory={path}", "-C", str(path), *argv]
        ).stdout.strip()

    receipt = verify_payload()
    if git("rev-parse", "HEAD") != SOURCE_COMMIT:
        raise ValueError("The installed source differs from the exact historical commit.")
    if git("rev-parse", "HEAD^{tree}") != receipt["source_tree"]:
        raise ValueError("The installed historical source tree differs from its package.")
    if git("status", "--porcelain", "--untracked-files=all"):
        raise ValueError("The historical source installation contains unexpected changes.")
    return {"commit": SOURCE_COMMIT, "tree": receipt["source_tree"]}


def integration_identity() -> dict:
    return {
        "files": {name: sha256(path) for name, path in INTEGRATION.items()},
        "current": os.readlink("/etc/rcp/current"),
        "installation_id": guest.read_json(guest.STATE / "historical.json")["installation_id"],
    }


def main_pid() -> int:
    return int(
        guest.run(["systemctl", "show", "rcp.service", "--property=MainPID", "--value"]).stdout
    )


def bootstrap() -> dict:
    guest.require_guest()
    if Path("/etc/rcp").exists() or Path("/home/rcp/rcp-server").exists():
        raise RuntimeError("Source adoption qualification requires a pristine disposable guest.")
    package = verify_payload()
    guest.run(["apt-get", "update"], timeout=300)
    guest.run(
        [
            "apt-get",
            "install",
            "--yes",
            "age",
            "ca-certificates",
            "curl",
            "git",
            "iproute2",
            "openssh-server",
            "sudo",
            "xz-utils",
            "libstdc++6",
        ],
        timeout=600,
    )
    guest.run(["install", "-m", "0755", str(guest.ROOT / "uv"), "/usr/local/bin/uv"])
    install_node()
    assert guest.run(["node", "--version"]).stdout.strip() == package["node_version"]
    assert guest.run(["npm", "--version"]).stdout.strip() == package["npm_version"]
    guest.run(
        [
            "git",
            "clone",
            "--branch",
            "main",
            str(PAYLOAD / "historical-source.bundle"),
            str(HISTORICAL),
        ]
    )
    source_identity(HISTORICAL)
    guest.run(
        [
            "uv",
            "venv",
            "--no-project",
            "--managed-python",
            "--python",
            "3.12",
            str(BOOTSTRAP.parent.parent),
        ],
        timeout=600,
    )
    bundle = guest.ROOT / "bundles/base"
    guest.run(
        [
            "uv",
            "pip",
            "sync",
            "--python",
            str(BOOTSTRAP),
            "--require-hashes",
            "--only-binary",
            ":all:",
            str(bundle / "requirements.lock.txt"),
        ],
        timeout=600,
    )
    for pattern in ("rcp-*.whl", "rcp_supervisor-*.whl"):
        (wheel,) = bundle.glob(pattern)
        guest.run(
            ["uv", "pip", "install", "--python", str(BOOTSTRAP), "--no-deps", str(wheel)],
            timeout=600,
        )
    historical = json.loads(
        guest.run([str(BOOTSTRAP), str(SCRIPT), "historical-setup"], timeout=2400).stdout
    )
    adopted = json.loads(
        guest.run([str(BOOTSTRAP), str(SCRIPT), "paired-bootstrap"], timeout=1800).stdout
    )
    assert adopted["status"] == "adopted"
    shutil.rmtree(BOOTSTRAP.parent.parent)
    result = json.loads(guest.run([guest.SUPERVISOR_PYTHON, str(SCRIPT), "verify"]).stdout)
    return {"status": "ready", "package": package, "historical": historical, "adoption": result}


def historical_setup() -> dict:
    sys.path.insert(0, str(HISTORICAL / "src"))
    import rcp
    from rcp.server_ops.config import (
        ServerSourceConfig,
        create_installed_server_config,
        write_installed_server_config,
    )
    from rcp.server_ops.install import LinuxInstallMachine, ManagedCheckout
    from rcp.server_ops.layout import DEFAULT_SERVER_LAYOUT as layout

    assert Path(rcp.__file__).resolve().is_relative_to(HISTORICAL / "src")
    machine = LinuxInstallMachine()
    machine.validate_host()
    machine.converge_account_and_layout()
    config = create_installed_server_config(
        source=ServerSourceConfig(origin=SOURCE_ORIGIN, authentication="public")
    )
    write_installed_server_config(config, layout.config_path)
    account = pwd.getpwnam("rcp")
    guest.STATE.mkdir(mode=0o700)
    os.chown(guest.STATE, account.pw_uid, account.pw_gid)
    guest.service(
        [
            "git",
            "clone",
            "--branch",
            "main",
            str(PAYLOAD / "historical-source.bundle"),
            str(layout.source_checkout),
        ]
    )
    guest.service(
        ["git", "-C", str(layout.source_checkout), "remote", "set-url", "origin", SOURCE_ORIGIN]
    )
    source_identity(layout.source_checkout)
    checkout = ManagedCheckout(commit=SOURCE_COMMIT, is_current_release=False)
    release = machine.build_release(checkout)
    assert release == RELEASE
    source_identity(release)
    state = machine.install_service(checkout, release)
    assert state.data_state == "fresh" and main_pid() == 0
    guest.service([str(RELEASE / ".venv/bin/python"), str(SCRIPT), "historical-data"])
    machine.activate_and_verify()
    health = guest.wait_health()
    assert health["running_commit"] == SOURCE_COMMIT and health["space_kind"] == "team"
    assert Path(f"/proc/{main_pid()}").stat().st_uid == account.pw_uid
    assert not Path("/etc/rcp/supervisor/selected.json").exists()
    backup = Path("/var/backups/rcp-qualification")
    backup.mkdir(mode=0o700)
    os.chown(backup, account.pw_uid, account.pw_gid)
    guest.run(
        [
            "/usr/local/bin/rcp",
            "server",
            "backup",
            "configure",
            "--destination",
            str(backup),
            "--schedule",
            "02:00",
            "--retention",
            "90",
            "--confirm",
            "--machine-readable",
        ],
        timeout=300,
    )
    guest.run(["systemctl", "disable", "--now", "rcp-backup.timer"])
    receipt = {
        "source_commit": SOURCE_COMMIT,
        "source_version": rcp.__version__,
        "installation_id": config.installation_id,
        "space_id": health["space_id"],
        "service_uid": account.pw_uid,
        "service_pid": main_pid(),
    }
    guest.write_json(guest.STATE / "historical.json", receipt)
    guest.write_json(guest.STATE / "old-integration.json", integration_identity())
    return receipt


def historical_data() -> dict:
    # The exact historical release interpreter owns every database write here.
    import rcp
    from tests.supervisor_reboot_data import prepare_data

    assert Path(rcp.__file__).resolve().is_relative_to(RELEASE / "src")
    fixture = prepare_data(DATA, Path("/home/rcp/rcp-server/projects"))
    guest.write_json(guest.STATE / "fixture.json", fixture, service_owned=True)
    return {"status": "prepared", "source_commit": SOURCE_COMMIT}


def fixture_release():
    from dataclasses import replace

    from rcp_supervisor.releases import verify_release

    receipt = guest.release_receipt(guest.ROOT / "bundles/base")
    return replace(
        verify_release(guest.ROOT / "bundles/base"),
        release_tag=receipt["release_tag"],
        full_commit=receipt["commit"],
    )


def paired_bootstrap() -> dict:
    """Run the app bootstrap CLI; its real subprocess delegates to our marked CLI wrapper."""
    from rcp_supervisor import releases

    from rcp import __main__ as app_cli
    from rcp.server_ops import install

    original = install.LinuxInstallMachine

    class ObservedMachine(original):
        def bootstrap_supervisor(self, bundle):
            result = super().bootstrap_supervisor(bundle)
            wrapper = Path("/usr/local/bin/rcp-supervisor")
            guest.write_json(guest.STATE / "supervisor-wrapper.json", {"text": wrapper.read_text()})
            # The real bootstrap delegate invokes this root-owned entry point.
            # Only the marked fixture replaces release provenance in that child.
            install._install_root_file(
                wrapper,
                f'#!/bin/sh\nset -eu\numask 077\nexec {guest.SUPERVISOR_PYTHON} {SCRIPT} supervisor "$@"\n',
                mode=0o755,
                replace_existing=True,
            )
            return result

        def stage_supervisor_integration(self):
            super().stage_supervisor_integration()
            assert integration_identity() == guest.read_json(guest.STATE / "old-integration.json")
            assert guest.health()["running_commit"] == SOURCE_COMMIT and main_pid() > 0
            guest.write_json(guest.STATE / "staged.json", {"active_source_preserved": True})

    install.LinuxInstallMachine = ObservedMachine
    releases.fetch_release = lambda *_args, **_kwargs: fixture_release()
    stream = io.StringIO()
    refusal = None
    previous = sys.argv
    sys.argv = [
        "rcp",
        "server",
        "install",
        "--team-name",
        "Reboot qualification",
        "--machine-readable",
    ]
    try:
        with redirect_stdout(stream):
            try:
                app_cli.main()
            except SystemExit as exc:
                if exc.code not in (None, 0):
                    refusal = exc
    finally:
        sys.argv = previous
        # Preserve parsed events privately even when the CLI exits with failure.
        # A truncated/non-JSON line must never replace the original exception or
        # become a public diagnostic that could contain terminal secrets.
        events = []
        for line in stream.getvalue().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                events.append(event)
        guest.write_json(guest.STATE / "bootstrap-events.json", {"events": events})
    if refusal is not None:
        diagnostic = bootstrap_failure(events, refusal.code)
        guest.write_json(guest.STATE / "bootstrap-failure.json", diagnostic)
        raise RuntimeError(
            "The paired-wheel bootstrap CLI refused adoption: " + diagnostic["message"]
        ) from refusal
    record = guest.read_json(Path("/etc/rcp/supervisor/adoption.json"))
    assert record["phase"] == "committed"
    wrapper = Path("/usr/local/bin/rcp-supervisor")
    install._install_root_file(
        wrapper,
        guest.read_json(guest.STATE / "supervisor-wrapper.json")["text"],
        mode=0o755,
        replace_existing=True,
    )
    return {"status": "adopted"}


def bootstrap_failure(events: list[dict], exit_code: object) -> dict:
    """Export only the failed step's declared message, never its fields/actions."""
    result = {
        "exit_code": exit_code if type(exit_code) is int else 1,
        "message": "No failed step message was emitted; inspect the private guest events.",
    }
    for event in reversed(events):
        step = event.get("step")
        if not isinstance(step, dict) or step.get("state") != "failed":
            continue
        message = step.get("message")
        if isinstance(message, str) and message.strip():
            result["message"] = message[:4000]
            phase = step.get("phase")
            if isinstance(phase, str):
                result["phase"] = phase[:128]
            break
    return result


def diagnostics() -> dict:
    """Read safe partial receipts with system Python even if bootstrap failed."""
    result = {"historical_install_completed": False}
    historical = guest.STATE / "historical.json"
    if historical.exists():
        document = guest.read_json(historical)
        result["historical_install"] = {
            name: document[name]
            for name in (
                "source_commit",
                "source_version",
                "installation_id",
                "space_id",
                "service_uid",
                "service_pid",
            )
        }
        result["historical_install_completed"] = True
    failure = guest.STATE / "bootstrap-failure.json"
    if failure.exists():
        document = guest.read_json(failure)
        result["bootstrap_failure"] = {
            name: document[name] for name in ("exit_code", "phase", "message") if name in document
        }
    return result


def supervisor(arguments: list[str]) -> int:
    from rcp_supervisor import cli, driver
    from rcp_supervisor.runtime import SystemRuntime

    class ObservedRuntime(SystemRuntime):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.notify = self.observe
            self.probe_verified = False

        def probe(self, release, operation, proof):
            assert main_pid() == 0 and not self.paths.selected.exists()
            super().probe(release, operation, proof)
            self.probe_verified = True

        def observe(self, phase):
            if not phase.startswith("adoption_"):
                return
            if phase == "adoption_guarded":
                assert (
                    "ExecStartPre=+/usr/local/bin/rcp-supervisor recover --startup"
                    in INTEGRATION["unit"].read_text()
                )
                assert os.readlink(self.paths.current) == str(RELEASE)
                assert not self.paths.selected.exists()
            if phase in {
                "adoption_stopped",
                "adoption_raw_checkpoint_ready",
                "adoption_checkpoint_ready",
                "adoption_probing",
                "adoption_candidate_chosen",
            }:
                assert main_pid() == 0 and not self.paths.selected.exists()
            if phase == "adoption_candidate_chosen":
                assert self.probe_verified
            guest.event(phase, guarded_selection_verified=True)

    driver.SystemRuntime = ObservedRuntime
    driver.followed_release = lambda _runtime: fixture_release()
    return cli.main(arguments)


def verify_protected_backup(record: dict, fixture: dict) -> dict:
    from rcp.server_ops.backup import BackupArchiveReceipt
    from rcp.server_ops.backup_identity import backup_identity_path
    from rcp.server_ops.restore import _extract_verified_archive
    from rcp.storage import AppStore

    protected = record["protected_backup"]
    archive = Path(protected["archive_path"])
    receipt_path = Path(protected["receipt_path"])
    assert sha256(archive) == protected["archive_sha256"]
    assert sha256(receipt_path) == protected["receipt_sha256"]
    receipt = BackupArchiveReceipt.model_validate_json(receipt_path.read_text())
    assert receipt.capture_status == "complete" and receipt.uncaptured_project_count == 0
    assert receipt.project_count == receipt.protected_project_count == 1
    assert receipt.space_id == fixture["space_id"] and receipt.readback == "passed"
    with archive.open("rb") as ciphertext:
        assert ciphertext.read(32).startswith(b"age-encryption.org/v1\n")
    private = guest.STATE / ("backup-verification-" + os.urandom(8).hex())
    private.mkdir(mode=0o700)
    plaintext = private / "backup.tar"
    try:
        guest.run(
            [
                "age",
                "--decrypt",
                "--identity",
                str(backup_identity_path()),
                "--output",
                str(plaintext),
                str(archive),
            ],
            timeout=120,
        )
        manifest, digest = _extract_verified_archive(
            plaintext, private / "payload", expected_uid=os.getuid(), expected_gid=os.getgid()
        )
        assert digest == receipt.manifest_sha256
        assert manifest.status == "complete" and manifest.space_id == fixture["space_id"]
        assert manifest.rcp_source_commit == SOURCE_COMMIT
        assert [project.project_id for project in manifest.projects] == [fixture["project_id"]]
        snapshot = AppStore.open_read_only_snapshot(private / "payload/database/rcp.sqlite3")
        assert (
            snapshot.authenticate_team_member_token(fixture["token"]).user_id
            == fixture["member_id"]
        )
    finally:
        shutil.rmtree(private)
    return {
        "capture_status": "complete",
        "project_count": 1,
        "uncaptured_project_count": 0,
        "decryption_and_inventory_verified": True,
        "archive_sha256": protected["archive_sha256"],
        "receipt_sha256": protected["receipt_sha256"],
    }


def verify() -> dict:
    import tomllib

    from rcp_supervisor.launch import read_selected_receipt

    health = guest.wait_health()
    selected = read_selected_receipt()
    assert selected == guest.release_receipt(guest.ROOT / "bundles/base")
    assert health["build"] == selected["build"] and health["running_commit"] == selected["commit"]
    assert health["version"] == selected["version_string"]
    historical = guest.read_json(guest.STATE / "historical.json")
    config = tomllib.loads(INTEGRATION["config"].read_text())
    assert config["schema_version"] == 3
    assert config["installation_id"] == historical["installation_id"]
    assert config["release"]["followed"] == "stable"
    fixture = guest.read_json(guest.STATE / "fixture.json")
    assert health["space_id"] == historical["space_id"] == fixture["space_id"]
    request = urllib.request.Request(
        "http://127.0.0.1:8421/api/projects",
        headers={
            "Cookie": guest.session_cookie(),
            "RCP-Team-Shell-Protocol": str(health["team_shell_protocol"]["maximum"]),
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        assert [project["id"] for project in json.load(response)] == [fixture["project_id"]]
    patches = Path(fixture["research"]) / "patches"
    assert {
        str(path.relative_to(patches)): sha256(path)
        for path in patches.rglob("*")
        if path.is_file()
    } == fixture["canonical_patches"]
    assert (
        Path(fixture["stage"]) / "retained.txt"
    ).read_bytes() == b"Retained paused Work scratch.\n"
    attachments = list((DATA / "chat-attachments").rglob(f"*{fixture['attachment_id']}.txt"))
    assert (
        len(attachments) == 1
        and attachments[0].read_bytes() == b"Retained attachment before deployment.\n"
    )
    account = pwd.getpwnam("rcp")
    for root in (DATA, Path(fixture["research"])):
        assert root.stat().st_uid == account.pw_uid
        assert all(path.lstat().st_uid == account.pw_uid for path in root.rglob("*"))
    installed = guest.verify_installation()
    guest.service(["/usr/local/bin/rcp", "server", "doctor", "--machine-readable"])
    assert Path("/etc/rcp/current").lstat().st_uid == 0
    assert (
        "ExecStartPre=+/usr/local/bin/rcp-supervisor recover --startup"
        in INTEGRATION["unit"].read_text()
    )
    assert guest.read_json(guest.STATE / "staged.json")["active_source_preserved"]
    record = guest.read_json(Path("/etc/rcp/supervisor/adoption.json"))
    assert record["phase"] == "committed" and record["previous"]["commit"] == SOURCE_COMMIT
    assert record["target"] == selected and record["raw_checkpoint"] and record["checkpoint"]
    operator = Path("/etc/rcp/supervisor/operator") / str(selected["build"]) / ".venv/bin/python"
    backup = json.loads(guest.run([str(operator), str(SCRIPT), "verify-backup"]).stdout)
    phases = [
        json.loads(line)["phase"]
        for line in (guest.STATE / "events.jsonl").read_text().splitlines()
    ]
    assert phases == [
        "adoption_" + phase
        for phase in (
            "prepared",
            "guarded",
            "stopped",
            "raw_checkpoint_ready",
            "checkpoint_ready",
            "probing",
            "candidate_chosen",
            "committed",
        )
    ]
    for path in INTEGRATION.values():
        info = path.stat()
        assert info.st_uid == 0 and not info.st_mode & 0o022
    source_identity(RELEASE)
    return {
        "status": "verified",
        "source_commit": SOURCE_COMMIT,
        "selected_release": selected,
        "space_id": health["space_id"],
        "member_reconnected": True,
        "canonical_history_preserved": True,
        "stage_and_attachment_preserved": True,
        "source_integration_preserved_until_guard": True,
        "protected_backup": backup,
        "service_uid": account.pw_uid,
        "service_pid": installed["service_pid"],
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
    }


def verify_backup() -> dict:
    return verify_protected_backup(
        guest.read_json(Path("/etc/rcp/supervisor/adoption.json")),
        guest.read_json(guest.STATE / "fixture.json"),
    )


def main() -> int:
    action, *arguments = sys.argv[1:]
    guest.require_guest(root=action not in {"historical-data", "supervisor"})
    if action == "supervisor":
        return supervisor(arguments)
    actions = {
        "bootstrap": bootstrap,
        "historical-setup": historical_setup,
        "historical-data": historical_data,
        "paired-bootstrap": paired_bootstrap,
        "verify": verify,
        "verify-backup": verify_backup,
        "diagnostics": diagnostics,
    }
    result = actions[action]()
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
