"""Package the exact pre-supervisor source and bounded Linux Node/npm runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from pathlib import Path

from tests.supervisor_adoption_guest import MAX_RUNTIME_BYTES, SOURCE_COMMIT, SOURCE_ORIGIN
from tests.supervisor_reboot_vm import run


def file_sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def source_bundle(origin: str, commit: str, destination: Path) -> str:
    """Fetch a commit into a private repository, never the caller's checkout."""
    environment = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    with tempfile.TemporaryDirectory(prefix="rcp-adoption-source-") as directory:
        repository = Path(directory)
        run(["git", "init", "--bare", str(repository)], env=environment)
        run(
            ["git", "-C", str(repository), "fetch", "--no-tags", origin, commit],
            env=environment,
            timeout=600,
        )
        observed = run(
            ["git", "-C", str(repository), "rev-parse", "FETCH_HEAD"], env=environment
        ).stdout.strip()
        if observed != commit:
            raise ValueError("Historical fetch did not return the exact source commit.")
        run(
            ["git", "-C", str(repository), "update-ref", "refs/heads/main", commit],
            env=environment,
        )
        run(
            ["git", "-C", str(repository), "bundle", "create", str(destination), "main"],
            env=environment,
            timeout=300,
        )
        run(
            ["git", "-C", str(repository), "bundle", "verify", str(destination)],
            env=environment,
        )
        return run(
            ["git", "-C", str(repository), "rev-parse", commit + "^{tree}"], env=environment
        ).stdout.strip()


def node_runtime(node: Path, destination: Path) -> dict:
    node = node.resolve(strict=True)
    npm = node.parent.parent / "lib/node_modules/npm"
    with node.open("rb") as executable:
        header = executable.read(20)
    if header[:4] != b"\x7fELF" or header[18:20] != b"\x3e\x00" or not npm.is_dir():
        raise ValueError("Adoption requires the workflow's Linux x86-64 Node/npm installation.")
    version = run([str(node), "--version"]).stdout.strip()
    npm_version = run([str(node), str(npm / "bin/npm-cli.js"), "--version"]).stdout.strip()
    if not version.startswith("v24."):
        raise ValueError("The historical installer requires Node.js 24.")
    total = node.stat().st_size
    for path in npm.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(npm.resolve()):
            raise ValueError("npm contains a link outside its own package.")
        if path.is_file() and not path.is_symlink():
            total += path.stat().st_size
    if total > MAX_RUNTIME_BYTES:
        raise ValueError("The Node/npm runtime exceeds the qualification payload bound.")
    with tarfile.open(destination, "w:gz", dereference=False) as archive:
        archive.add(node, arcname="bin/node", recursive=False)
        archive.add(npm, arcname="lib/node_modules/npm")
        link = tarfile.TarInfo("bin/npm")
        link.type = tarfile.SYMTYPE
        link.linkname = "../lib/node_modules/npm/bin/npm-cli.js"
        link.mode = 0o755
        archive.addfile(link)
    return {"node_version": version, "npm_version": npm_version, "runtime_bytes": total}


def build_payload(output: Path) -> dict:
    if output.exists():
        raise ValueError("Adoption payload output must be a new directory.")
    node = shutil.which("node")
    if node is None:
        raise ValueError("Set up Node.js 24 before packaging the historical installation.")
    output.mkdir(mode=0o700, parents=True)
    runtime = node_runtime(Path(node), output / "node-runtime.tar.gz")
    tree = source_bundle(SOURCE_ORIGIN, SOURCE_COMMIT, output / "historical-source.bundle")
    receipt = {
        "source_origin": SOURCE_ORIGIN,
        "source_commit": SOURCE_COMMIT,
        "source_tree": tree,
        **runtime,
        "files": {
            name: file_sha256(output / name)
            for name in ("historical-source.bundle", "node-runtime.tar.gz")
        },
    }
    (output / "package-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(build_payload(arguments.output.resolve())))


if __name__ == "__main__":
    main()
