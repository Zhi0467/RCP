"""Private full-update rehearsal in an owned disposable Linux VM.

Snapshots and guest evidence stay in a private operator directory. Only code,
verified release bundles and dependencies enter the guest before egress is disabled.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.supervisor_reboot_vm import (  # noqa: E402
    GUEST_ROOT,
    Guest,
    download_image,
    preflight,
    run,
)

PRIVATE_ROOT = "/home/qualifier/private-state"
GUEST_OUTPUT = "/home/qualifier/private-output"
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def tree_digest(root: Path) -> str:
    """Hash relative names, bytes, metadata, special nodes and in-tree hardlinks."""
    entries = []
    links: dict[tuple[int, int], list[str]] = {}

    def visit(path: Path) -> None:
        info = path.lstat()
        name = path.relative_to(root).as_posix()
        content = None
        if stat.S_ISREG(info.st_mode):
            content = file_digest(path)
            links.setdefault((info.st_dev, info.st_ino), []).append(name)
        elif stat.S_ISLNK(info.st_mode):
            content = os.readlink(path)
        elif stat.S_ISDIR(info.st_mode):
            for child in sorted(path.iterdir()):
                visit(child)
        elif not (stat.S_ISFIFO(info.st_mode) or stat.S_ISSOCK(info.st_mode)):
            raise ValueError("Private snapshots cannot contain device nodes.")
        entries.append(
            (
                name,
                info.st_mode,
                info.st_uid,
                info.st_gid,
                info.st_mtime_ns,
                content,
                {
                    key: os.getxattr(path, key, follow_symlinks=False).hex()
                    for key in sorted(os.listxattr(path, follow_symlinks=False))
                }
                if sys.platform == "linux"
                else None,
            )
        )

    visit(root)
    groups = sorted(sorted(names) for names in links.values() if len(names) > 1)
    return hashlib.sha256(json.dumps([entries, groups], sort_keys=True).encode()).hexdigest()


def _absolute(value: str) -> Path:
    if not isinstance(value, str) or not value.startswith("/"):
        raise ValueError("Private snapshot paths and destinations must be absolute.")
    path = Path(value)
    if path == Path("/") or ".." in path.parts or str(path) != value:
        raise ValueError("Private snapshot paths and destinations must be normalized.")
    return path


def validate_private_state(path: Path, source_digest: str) -> dict:
    state = json.loads(path.read_text())
    if (
        not isinstance(state, dict)
        or type(state.get("version")) is not int
        or state["version"] != 1
        or not _HEX.fullmatch(state.get("source_manifest_sha256", ""))
        or state["source_manifest_sha256"] != source_digest
        or not isinstance(state.get("data"), dict)
        or not isinstance(state.get("local_roots"), list)
    ):
        raise ValueError("Private state must bind the verified source manifest and snapshot roots.")
    roots = [state["data"], *state["local_roots"]]
    destinations = []
    for root in roots:
        if not isinstance(root, dict):
            raise ValueError("Private snapshot roots must be objects.")
        snapshot, destination = _absolute(root.get("snapshot")), _absolute(root.get("destination"))
        if snapshot.is_symlink() or not snapshot.is_dir():
            raise ValueError("Private snapshot root must be an existing real directory.")
        if (
            not _HEX.fullmatch(root.get("tree_sha256", ""))
            or tree_digest(snapshot) != root["tree_sha256"]
        ):
            raise ValueError("Private snapshot tree digest changed.")
        if any(
            destination == other or destination in other.parents or other in destination.parents
            for other in destinations
        ):
            raise ValueError("Private snapshot destinations must not overlap.")
        destinations.append(destination)
    if destinations[0] != Path("/home/rcp/rcp-server/data"):
        raise ValueError("Private data must retain the installed server data destination.")
    expected = state.get("expected", {})
    if not isinstance(expected, dict):
        raise ValueError("Private state must name expected identities.")
    projects = expected.get("project_ids")
    if (
        not isinstance(expected.get("space_id"), str)
        or not expected["space_id"]
        or not isinstance(projects, list)
        or not all(isinstance(p, str) and p for p in projects)
        or len(projects) != len(set(projects))
        or not isinstance(expected.get("canonical_files"), dict)
    ):
        raise ValueError(
            "Private state must name expected space, project and canonical identities."
        )
    for name, digest in expected["canonical_files"].items():
        canonical = _absolute(name)
        matching = [root for root in roots if Path(root["destination"]) in canonical.parents]
        if len(matching) != 1 or not isinstance(digest, str) or not _HEX.fullmatch(digest):
            raise ValueError("Private canonical file must belong to exactly one snapshot root.")
        root = matching[0]
        source = Path(root["snapshot"]) / canonical.relative_to(root["destination"])
        if (
            not source.resolve().is_relative_to(Path(root["snapshot"]).resolve())
            or source.is_symlink()
            or not source.is_file()
            or file_digest(source) != digest
        ):
            raise ValueError("Private canonical file digest changed.")
    state["input_sha256"] = state.get("input_sha256", file_digest(path))
    if not isinstance(state["input_sha256"], str) or not _HEX.fullmatch(state["input_sha256"]):
        raise ValueError("Private input digest is malformed.")
    return state


def bundle_identity(bundle: Path) -> dict:
    from rcp_supervisor.releases import verify_release

    verified = verify_release(bundle)
    receipt = json.loads(bundle.with_name(bundle.name + ".receipt.json").read_text())
    commit = receipt.get("full_commit", "")
    if (
        receipt.get("manifest_sha256") != verified.manifest_sha256
        or not isinstance(commit, str)
        or not _COMMIT.fullmatch(commit)
        or not commit.startswith(verified.commit)
        or receipt.get("tag") != "v" + verified.version.split("+", 1)[0]
    ):
        raise ValueError("Local release receipt disagrees with its verified bundle.")
    return {
        "manifest_sha256": verified.manifest_sha256,
        "commit": commit,
        "version": verified.version,
        "supervisor_version": verified.supervisor_version,
    }


def validate_report(report: dict, source: dict, candidate: dict, input_digest: str) -> None:
    required = {
        "version": 1,
        "journey": "installed-update",
        "status": "passed",
        "input_sha256": input_digest,
        "supervisor_version": candidate["supervisor_version"],
        "rollback_exact": True,
        "retry_committed": True,
    }
    if not isinstance(report, dict) or any(
        not isinstance(report.get(key), dict)
        for key in ("source", "candidate", "journal", "serving")
    ):
        raise ValueError("Guest report must contain complete identity records.")
    journal = {
        "phase": "committed",
        "previous_manifest_sha256": source["manifest_sha256"],
        "target_manifest_sha256": candidate["manifest_sha256"],
    }
    serving = {"status": "ok", "commit": candidate["commit"], "version": candidate["version"]}
    if (
        any(
            report.get(key) != value or type(report.get(key)) is not type(value)
            for key, value in required.items()
        )
        or report.get("procedure") not in {"graceful", "stopped-source"}
        or any(
            report.get("source", {}).get(key) != source[key]
            for key in ("manifest_sha256", "commit", "version")
        )
        or any(report.get("candidate", {}).get(key) != value for key, value in candidate.items())
        or any(report["journal"].get(key) != value for key, value in journal.items())
        or any(report["serving"].get(key) != value for key, value in serving.items())
    ):
        raise ValueError("Guest report does not prove the complete installed update.")


def code_payload(base: Path, candidate: Path, destination: Path) -> None:
    workspace = Path(__file__).resolve().parents[1]
    uv = shutil.which("uv")
    if uv is None:
        raise ValueError("uv is required to provision the guest.")
    with tarfile.open(destination, "w:gz") as archive:
        for source, name in ((base, "base"), (candidate, "target")):
            archive.add(source, arcname=f"bundles/{name}")
            archive.add(
                source.with_name(source.name + ".receipt.json"),
                arcname=f"bundles/{name}.receipt.json",
            )
        for source in sorted((workspace / "tests").glob("*.py")):
            archive.add(source, arcname=f"tests/{source.name}")
        archive.add(Path(uv).resolve(strict=True), arcname="uv")


def private_payload(state: dict, output: Path) -> dict:
    roots = [state["data"], *state["local_roots"]]
    sources = [Path(root["snapshot"]) for root in roots]
    sockets = []
    parents = {}
    for source in sources:
        for path in source.rglob("*"):
            info = path.lstat()
            if stat.S_ISSOCK(info.st_mode):
                sockets.append(
                    {
                        "path": str(path).lstrip("/"),
                        "mode": stat.S_IMODE(info.st_mode),
                        "uid": info.st_uid,
                        "gid": info.st_gid,
                        "mtime_ns": info.st_mtime_ns,
                        "xattrs": {
                            key: os.getxattr(path, key, follow_symlinks=False).hex()
                            for key in os.listxattr(path, follow_symlinks=False)
                        },
                    }
                )
                parents[str(path.parent).lstrip("/")] = path.parent.stat().st_mtime_ns
    run(
        [
            "tar",
            "--create",
            "--format=pax",
            "--sparse",
            "--acls",
            "--xattrs",
            "--xattrs-include=*",
            "--numeric-owner",
            "--file",
            str(output / "private.tar"),
            "--directory",
            "/",
            "--",
            *(str(source).lstrip("/") for source in sources),
        ],
        timeout=1800,
    )
    transported = json.loads(json.dumps(state))
    for root in [transported["data"], *transported["local_roots"]]:
        root["snapshot"] = PRIVATE_ROOT + root["snapshot"]
    (output / "private-sockets.json").write_text(
        json.dumps({"sockets": sockets, "parents": parents})
    )
    (output / "private-state.json").write_text(json.dumps(transported))
    return transported


def unpack_private(token: str) -> None:
    """Recreate the special nodes GNU tar omits, then let the guest driver verify it."""
    from tests.server_installed_upgrade import require_guest

    if sys.platform != "linux" or os.geteuid() != 0:
        raise ValueError("Private transport requires the owned disposable guest.")
    require_guest(token)
    root = Path(PRIVATE_ROOT)
    root.mkdir(mode=0o700)
    run(
        [
            "tar",
            "--extract",
            "--sparse",
            "--acls",
            "--xattrs",
            "--xattrs-include=*",
            "--same-owner",
            "--file",
            str(root.parent / "private.tar"),
            "--directory",
            str(root),
        ],
        timeout=1800,
    )
    records = json.loads((root.parent / "private-sockets.json").read_text())
    for record in records["sockets"]:
        path = root / record["path"]
        previous = Path.cwd()
        try:
            os.chdir(path.parent)
            with socket.socket(socket.AF_UNIX) as stream:
                stream.bind(path.name)
        finally:
            os.chdir(previous)
        os.chown(path, record["uid"], record["gid"])
        path.chmod(record["mode"])
        for key, value in record["xattrs"].items():
            os.setxattr(path, key, bytes.fromhex(value), follow_symlinks=False)
        os.utime(path, ns=(record["mtime_ns"], record["mtime_ns"]))
    for name, mtime in records["parents"].items():
        os.utime(root / name, ns=(mtime, mtime))


def drive(
    base: Path,
    candidate: Path,
    private_state: Path,
    output: Path,
    confirmation: str,
    *,
    ubuntu: str = "24.04",
) -> None:
    source, target = bundle_identity(base), bundle_identity(candidate)
    state = validate_private_state(private_state, source["manifest_sha256"])
    state["input_sha256"] = file_digest(private_state)
    if any(
        output.resolve().is_relative_to(Path(root["snapshot"]).resolve())
        for root in (state["data"], *state["local_roots"])
    ):
        raise ValueError("Private output must be outside the captured snapshots.")
    output.mkdir(mode=0o700)
    if output.is_symlink() or output.stat().st_uid != os.geteuid():
        raise ValueError("Private output must be a new operator-owned directory.")
    preflight(output, confirmation, operator_host=True)
    image, digest = download_image(output, ubuntu)
    payload = output / "code.tar.gz"
    code_payload(base, candidate, payload)
    guest = Guest(output / "guest", image=image, image_sha256=digest, ubuntu=ubuntu)
    script = f"{GUEST_ROOT}/tests/server_installed_upgrade.py"
    arguments = [
        "--base",
        f"{GUEST_ROOT}/bundles/base",
        "--candidate",
        f"{GUEST_ROOT}/bundles/target",
        "--tag",
        "v" + source["version"].split("+", 1)[0],
        "--output",
        GUEST_OUTPUT,
        "--guest-token",
        guest.token,
    ]
    try:
        guest.start()
        guest.install_payload(payload)
        guest.ssh(["sudo", "-n", "apt-get", "update"], timeout=600)
        guest.ssh(
            [
                "sudo",
                "-n",
                "apt-get",
                "install",
                "--yes",
                "age",
                "ca-certificates",
                "curl",
                "git",
                "iproute2",
                "sudo",
                "xz-utils",
                "util-linux",
            ],
            timeout=900,
        )
        guest.ssh(["sudo", "-n", "install", "-m", "0755", f"{GUEST_ROOT}/uv", "/usr/local/bin/uv"])
        owner = Path(state["data"]["snapshot"]).stat()
        guest.ssh(
            [
                "sudo",
                "-n",
                "python3",
                script,
                *arguments,
                "--prepare-guest",
                "--service-uid",
                str(owner.st_uid),
                "--service-gid",
                str(owner.st_gid),
            ],
            timeout=3600,
        )
        guest.ssh(["sudo", "-n", "sync"])
        guest.power_cycle(offline=True)
        private_payload(state, output)
        validate_private_state(private_state, source["manifest_sha256"])
        for name in ("private.tar", "private-sockets.json", "private-state.json"):
            guest.copy(output / name, "/home/qualifier/" + name)
        python = "/opt/rcp-installed-upgrade/bootstrap/bin/python"
        guest.ssh(
            [
                "sudo",
                "-n",
                python,
                f"{GUEST_ROOT}/tests/server_update_rehearsal.py",
                "--unpack-private",
                "--guest-token",
                guest.token,
            ],
            timeout=1800,
        )
        guest.ssh(
            [
                "sudo",
                "-n",
                python,
                script,
                *arguments,
                "--private-state",
                "/home/qualifier/private-state.json",
            ],
            timeout=3600,
        )
        raw = guest.ssh(["sudo", "-n", "cat", f"{GUEST_OUTPUT}/result.json"]).stdout
        (output / "guest-result.json").write_text(raw)
        report = json.loads(raw)
        validate_report(report, source, target, state["input_sha256"])
        if file_digest(private_state) != state["input_sha256"]:
            raise ValueError("Private input manifest changed during the rehearsal.")
    finally:
        try:
            guest.collect(
                "systemd.log", ["sudo", "-n", "journalctl", "-u", "rcp.service", "--no-pager"]
            )
            guest.collect(
                "operator-events.jsonl",
                ["sudo", "-n", "cat", "/opt/rcp-installed-upgrade/operator-events.jsonl"],
            )
        finally:
            guest.power_off()
    (output / "result.json").write_text(raw)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unpack-private", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--guest-token", help=argparse.SUPPRESS)
    for name in ("base", "candidate", "private-state", "output"):
        parser.add_argument(f"--{name}", type=Path)
    parser.add_argument("--confirm-disposable")
    parser.add_argument("--ubuntu", choices=("22.04", "24.04"), default="24.04")
    args = parser.parse_args()
    if args.unpack_private:
        unpack_private(args.guest_token)
        return
    if not all(
        (args.base, args.candidate, args.private_state, args.output, args.confirm_disposable)
    ):
        parser.error("base, candidate, private-state, output and confirm-disposable are required")
    drive(
        args.base,
        args.candidate,
        args.private_state,
        args.output,
        args.confirm_disposable,
        ubuntu=args.ubuntu,
    )


if __name__ == "__main__":
    main()
