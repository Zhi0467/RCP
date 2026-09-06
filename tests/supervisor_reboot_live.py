"""Disposable install, protected restore, and externally observed real reboot qualification."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tarfile
from dataclasses import asdict, dataclass
from pathlib import Path

from tests.supervisor_reboot_vm import (
    GUEST_ROOT,
    Guest,
    QualificationUnavailable,
    download_image,
    preflight,
)


@dataclass(frozen=True)
class Scenario:
    name: str
    kind: str
    pauses: tuple[str, ...]
    offline: bool
    force_rollback: bool = False


def scenarios() -> tuple[Scenario, ...]:
    forward = (
        "checkpoint_ready",
        "activating",
        "pointer_switched",
        "candidate_verified",
        "candidate_chosen",
        "committed",
    )
    rollback = (
        "rollback_started",
        "root_quarantined:0",
        "root_published:0",
        "root_quarantined:1",
        "root_published:1",
        "rollback_roots_complete",
        "previous_pointer_restored",
        "previous_verified",
        "previous_chosen",
        "rolled_back",
    )
    result = []
    for offline in (False, True):
        network = "offline" if offline else "online"
        for kind in ("update", "restore"):
            publication = (
                tuple(
                    f"candidate_root_{action}:{index}"
                    for index in (0, 1)
                    for action in ("quarantined", "published")
                )
                if kind == "restore"
                else ()
            )
            for phase in (*forward, *publication, *rollback):
                result.append(
                    Scenario(
                        f"{kind}-{phase.replace(':', '-')}-{network}",
                        kind,
                        (phase,),
                        offline,
                        phase in rollback,
                    )
                )
            result.append(
                Scenario(
                    f"{kind}-repeated-rollback-{network}",
                    kind,
                    ("candidate_verified", "root_quarantined:0"),
                    offline,
                )
            )
        result.append(Scenario(f"post-admission-{network}", "update", ("post_admission",), offline))
        result.append(Scenario(f"invalid-journal-{network}", "invalid", (), offline))
        for phase in ("candidate_root_published:1", "candidate_chosen"):
            result.append(
                Scenario(
                    f"fresh-restore-{phase.replace(':', '-')}-{network}",
                    "fresh_restore",
                    (phase,),
                    offline,
                )
            )
    return tuple(result)


def write_receipt(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def payload(
    workspace: Path, bundles: Path, destination: Path, *, adoption: Path | None = None
) -> None:
    with tarfile.open(destination, "w:gz") as archive:
        for name in ("base", "target", "build-receipt.json"):
            archive.add(bundles / name, arcname=f"bundles/{name}")
        for name in ("__init__.py", "supervisor_reboot_guest.py", "supervisor_reboot_data.py"):
            archive.add(workspace / "tests" / name, arcname=f"tests/{name}")
        if adoption is not None:
            for name in ("historical-source.bundle", "node-runtime.tar.gz", "package-receipt.json"):
                archive.add(adoption / name, arcname=f"adoption/{name}")
            archive.add(
                workspace / "tests/supervisor_adoption_guest.py",
                arcname="tests/supervisor_adoption_guest.py",
            )
        uv = shutil.which("uv")
        if uv is None:
            raise RuntimeError("uv disappeared after the qualification build.")
        archive.add(Path(uv).resolve(strict=True), arcname="uv")


def drive(ubuntu: str, bundles: Path, output: Path) -> None:
    from tests.helpers import wait_until

    workspace = Path(__file__).resolve().parents[1]
    supported = preflight(output, os.environ.get("RCP_REBOOT_DISPOSABLE", ""))
    write_receipt(output / "drive-preflight.json", {"status": "supported", **supported})
    image, digest = download_image(output, ubuntu)
    upload = output / "payload.tar.gz"
    payload(workspace, bundles, upload)
    guest = Guest(output / "guest", image=image, image_sha256=digest, ubuntu=ubuntu)
    receipt = {
        "status": "running",
        "actual_reboot_proven": False,
        "ubuntu": ubuntu,
        "image_sha256": digest,
        "build_receipt": json.loads((bundles / "build-receipt.json").read_text()),
        "preflight": supported,
        "cases": [],
    }
    write_receipt(output / "qualification.json", receipt)
    guest_script = f"{GUEST_ROOT}/tests/supervisor_reboot_guest.py"

    def command(*argv: str, timeout: int = 120) -> dict:
        result = guest.ssh(
            ["sudo", "-n", "/etc/rcp/supervisor/current/bin/python", guest_script, *argv],
            timeout=timeout,
        )
        return json.loads(result.stdout)

    try:
        guest.start()
        guest.install_payload(upload)
        receipt["bootstrap"] = json.loads(
            guest.ssh(["sudo", "-n", "python3", guest_script, "bootstrap"], timeout=1800).stdout
        )
        guest.save_baseline()
        for case in scenarios():
            print(f"Starting {case.name}", flush=True)
            initial_boot = guest.reset()
            case_file = output / "case.json"
            write_receipt(case_file, asdict(case))
            guest.copy(case_file, "/home/qualifier/case.json")
            command("prepare-case", "/home/qualifier/case.json")
            case_receipt = {
                "name": case.name,
                "kind": case.kind,
                "offline_recovery": case.offline,
                "boot_ids": [initial_boot],
                "interruptions": [],
            }
            if case.kind == "invalid":
                case_receipt["boot_ids"].append(guest.power_cycle(offline=case.offline))
            else:
                guest.ssh(
                    [
                        "sudo",
                        "-n",
                        "systemd-run",
                        "--unit=rcp-qualification-drive",
                        "--collect",
                        "/etc/rcp/supervisor/current/bin/python",
                        guest_script,
                        "run-case",
                    ]
                )
                for index, phase in enumerate(case.pauses):

                    def paused(index=index, phase=phase):
                        status = command("status")
                        if status.get("error"):
                            raise RuntimeError(status["error"])
                        return (
                            status
                            if status.get("pause_index") == index and status.get("phase") == phase
                            else None
                        )

                    event = wait_until(
                        paused,
                        timeout=240,
                        interval=2,
                        detail=f"Supervisor never reached {case.name} boundary {phase}.",
                    )
                    if phase == "post_admission":
                        event["accepted_work"] = command("accept-work")
                    case_receipt["interruptions"].append(event)
                    case_receipt["boot_ids"].append(guest.power_cycle(offline=case.offline))
            case_receipt["verification"] = command("verify-case", timeout=240)
            guest.collect(
                f"{case.name}.log",
                [
                    "sudo",
                    "-n",
                    "journalctl",
                    "-u",
                    "rcp.service",
                    "-u",
                    "rcp-qualification-drive.service",
                    "--no-pager",
                    "-o",
                    "short-monotonic",
                ],
            )
            receipt["cases"].append(case_receipt)
            write_receipt(output / "qualification.json", receipt)
            print(f"Verified {case.name}", flush=True)
        receipt["status"] = "passed"
        receipt["actual_reboot_proven"] = True
    except Exception as exc:
        receipt["status"] = "failed"
        receipt["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        try:
            if guest.process is not None and guest.process.poll() is None:
                if receipt["status"] == "failed":
                    try:
                        receipt["failure_inventory"] = command("diagnostics", timeout=75)
                    except Exception as diagnostic_error:
                        receipt["diagnostic_collection_error"] = type(diagnostic_error).__name__
                guest.collect(
                    "systemd.log",
                    [
                        "sudo",
                        "-n",
                        "journalctl",
                        "-u",
                        "rcp.service",
                        "-u",
                        "rcp-qualification-drive.service",
                        "--no-pager",
                        "-o",
                        "short-monotonic",
                    ],
                )
                guest.collect(
                    "events.json",
                    ["sudo", "-n", "cat", f"{GUEST_ROOT}/fault-state/events.jsonl"],
                )
        finally:
            guest.power_off()
            write_receipt(output / "qualification.json", receipt)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("preflight")
    check.add_argument("--output", type=Path, required=True)
    live = commands.add_parser("drive")
    live.add_argument("--ubuntu", choices=("22.04", "24.04"), required=True)
    live.add_argument("--bundles", type=Path, required=True)
    live.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    arguments.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        if arguments.command == "preflight":
            receipt = preflight(arguments.output, os.environ.get("RCP_REBOOT_DISPOSABLE", ""))
            write_receipt(arguments.output / "preflight.json", {"status": "supported", **receipt})
        else:
            drive(arguments.ubuntu, arguments.bundles, arguments.output)
    except QualificationUnavailable as exc:
        write_receipt(
            arguments.output
            / ("preflight.json" if arguments.command == "preflight" else "drive-preflight.json"),
            {
                "status": "qualification-unavailable",
                "reason": str(exc),
                "actual_reboot_proven": False,
            },
        )
        print(f"Qualification unavailable: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
