"""Drive historical source adoption and an offline real reboot in a pristine guest."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from tests.supervisor_reboot_live import payload, write_receipt
from tests.supervisor_reboot_vm import (
    GUEST_ROOT,
    Guest,
    QualificationUnavailable,
    download_image,
    preflight,
)


def drive(ubuntu: str, bundles: Path, adoption: Path, output: Path) -> None:
    supported = preflight(output, os.environ.get("RCP_REBOOT_DISPOSABLE", ""))
    write_receipt(output / "preflight.json", {"status": "supported", **supported})
    image, digest = download_image(output, ubuntu)
    upload = output / "payload.tar.gz"
    payload(Path(__file__).resolve().parents[1], bundles, upload, adoption=adoption)
    guest = Guest(output / "guest", image=image, image_sha256=digest, ubuntu=ubuntu)
    script = f"{GUEST_ROOT}/tests/supervisor_adoption_guest.py"
    receipt = {
        "status": "running",
        "actual_reboot_proven": False,
        "ubuntu": ubuntu,
        "image_sha256": digest,
        "build_receipt": json.loads((bundles / "build-receipt.json").read_text()),
        "source_payload": json.loads((adoption / "package-receipt.json").read_text()),
        "preflight": supported,
        "boot_ids": [],
    }
    write_receipt(output / "qualification.json", receipt)
    try:
        receipt["boot_ids"].append(guest.start())
        guest.install_payload(upload)
        receipt["adoption"] = json.loads(
            guest.ssh(["sudo", "-n", "python3", script, "bootstrap"], timeout=3600).stdout
        )
        write_receipt(output / "qualification.json", receipt)
        receipt["boot_ids"].append(guest.power_cycle(offline=True))
        receipt["after_offline_reboot"] = json.loads(
            guest.ssh(
                ["sudo", "-n", "/etc/rcp/supervisor/current/bin/python", script, "verify"],
                timeout=240,
            ).stdout
        )
        receipt["actual_reboot_proven"] = True
        receipt["status"] = "passed"
    except Exception as exc:
        receipt["status"] = "failed"
        receipt["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        try:
            if guest.process is not None and guest.process.poll() is None:
                if receipt["status"] == "failed":
                    try:
                        receipt["partial_evidence"] = json.loads(
                            guest.ssh(
                                ["sudo", "-n", "python3", script, "diagnostics"], timeout=60
                            ).stdout
                        )
                    except Exception as diagnostic_error:
                        # Do not replace the drive failure or publish raw output.
                        receipt["diagnostic_collection_error"] = type(diagnostic_error).__name__
                try:
                    guest.collect(
                        "systemd.log",
                        ["sudo", "-n", "journalctl", "-u", "rcp.service", "--no-pager"],
                    )
                except Exception as collection_error:
                    receipt["systemd_collection_error"] = type(collection_error).__name__
                    if receipt["status"] != "failed":
                        receipt["status"] = "failed"
                        receipt["error"] = "Systemd receipt collection failed."
                        raise
        finally:
            guest.power_off()
            write_receipt(output / "qualification.json", receipt)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ubuntu", choices=("22.04", "24.04"), required=True)
    parser.add_argument("--bundles", type=Path, required=True)
    parser.add_argument("--adoption", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    arguments.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    try:
        drive(arguments.ubuntu, arguments.bundles, arguments.adoption, arguments.output)
    except QualificationUnavailable as exc:
        write_receipt(
            arguments.output / "preflight.json",
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
