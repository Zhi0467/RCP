"""Disposable-runner instrumentation, copied as root-owned sitecustomize.py.

Only artifact selection and observation/fault injection are substituted. The
installed shell wrappers, CLI, runtime, subprocesses and systemd unit run normally.
This file is never included in either release wheel.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path("/opt/rcp-installed-upgrade")


def permission_state() -> dict:
    """Read the kernel mask without temporarily changing process-wide state."""
    mask = next(
        line.split()[1]
        for line in Path("/proc/self/status").read_text().splitlines()
        if line.startswith("Umask:")
    )
    return {"pid": os.getpid(), "uid": os.geteuid(), "gid": os.getegid(), "umask": mask}


def service_permissions(releases: Path = Path("/home/rcp/rcp-server/releases")) -> dict:
    result = permission_state()
    # Default ACLs can override umask even below a chmod-0700 directory. Use
    # the same mkdir modes as release preparation, without touching a build.
    with tempfile.TemporaryDirectory(prefix=".permission-probe-", dir=releases) as name:
        private = Path(name)
        directory = private / "directory"
        directory.mkdir()
        file = directory / "file"
        file.touch()
        result["directory_mode"] = oct(stat.S_IMODE(directory.stat().st_mode))
        result["file_mode"] = oct(stat.S_IMODE(file.stat().st_mode))
        result["default_acls"] = {
            str(path): os.getxattr(path, "system.posix_acl_default").hex()
            for path in (releases, private, directory)
            if "system.posix_acl_default" in os.listxattr(path)
        }
    return result


def observe_uv_launch(event: str, arguments: tuple) -> None:
    if event == "subprocess.Popen" and Path(os.fsdecode(arguments[0])).name == "uv":
        print("Installed upgrade uv parent: " + json.dumps(permission_state()), file=sys.stderr)


def install_hooks() -> None:
    from rcp_supervisor import driver
    from rcp_supervisor.operations import Coordinator
    from rcp_supervisor.releases import verify_release
    from rcp_supervisor.runtime import DEFAULT_PATHS, SystemRuntime

    sys.path.insert(0, str(ROOT))
    from tests.server_upgrade_scratch import tree_state

    def followed_release(runtime):
        permissions = {
            "coordinator": permission_state(),
            "service_child": runtime.service_json(
                [sys.executable, "-I", str(ROOT / "tests/server_installed_upgrade_hook.py")]
            ),
        }
        with (ROOT / "permission-state.jsonl").open("a") as output:
            output.write(json.dumps(permissions) + "\n")
        print("Installed upgrade permissions: " + json.dumps(permissions), file=sys.stderr)
        selection = json.loads((ROOT / "selection.json").read_text())
        bundle = Path(selection["bundle"])
        provenance = json.loads(bundle.with_name(bundle.name + ".receipt.json").read_text())
        release = verify_release(bundle)
        assert release.manifest_sha256 == provenance["manifest_sha256"]
        assert provenance["full_commit"].startswith(release.commit)
        return replace(
            release, release_tag=provenance["tag"], full_commit=provenance["full_commit"]
        )

    # Both systemd and the fenced probation process use the disposable port.
    # All filesystem paths, ownership checks and runtime operations remain real.
    runtime_initialize = SystemRuntime.__init__

    def initialize_runtime(self, paths=DEFAULT_PATHS, **kwargs):
        runtime_initialize(self, replace(paths, port=18421), **kwargs)

    SystemRuntime.__init__ = initialize_runtime
    original = Coordinator.__init__

    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        original_boundary = self.boundary

        def boundary(phase):
            original_boundary(phase)
            selection = json.loads((ROOT / "selection.json").read_text())
            if not selection.get("observe"):
                return
            operation = self.store.active()
            if operation is None:
                # Terminal records are no longer active.
                return
            oracle = ROOT / f"oracle-{operation['operation_id']}.json"
            if phase in {"snapshot_ready", "rollback_roots_complete"}:
                catalog = json.loads(
                    (Path(operation["checkpoint"]["directory"]) / "checkpoint.json").read_text()
                )
                roots = tuple(Path(item["live"]) for item in catalog["roots"])
                facts = json.dumps(tree_state(roots), sort_keys=True)
                if phase == "snapshot_ready":
                    oracle.write_text(facts)
                else:
                    assert facts == oracle.read_text(), "rollback changed the stopped filesystem"
                    (ROOT / "rollback-exact.json").write_text(
                        json.dumps({"operation_id": operation["operation_id"], "roots": len(roots)})
                    )
            if phase == "candidate_verified" and selection.get("fail"):
                assert oracle.is_file(), "fault must follow a durable whole-root snapshot"
                # Prove restoration of additions, edits, removals and empty directories,
                # in addition to the candidate's actual SQLite/startup mutations.
                data = self.runtime.paths.data_dir
                (data / "run-stage/real-tools/repo/result.txt").write_text("candidate mutation\n")
                (data / "run-stage/real-tools/empty").rmdir()
                (data / "providers/claude/qualification/setup-token").unlink()
                added = data / "candidate-only"
                added.write_text("must disappear\n")
                owner = data.stat()
                os.chown(added, owner.st_uid, owner.st_gid)
                raise RuntimeError("installed-upgrade intentional candidate failure")

        self.boundary = boundary

    driver.followed_release = followed_release
    Coordinator.__init__ = initialize


# Python normally prints and ignores sitecustomize errors. A missing test hook
# must instead fail closed before the CLI can choose a public release.
if __name__ == "sitecustomize":
    try:
        sys.addaudithook(observe_uv_launch)
        if os.geteuid() == 0:
            install_hooks()
    except Exception as exc:
        print(f"Installed upgrade instrumentation failed: {exc}", file=sys.stderr)
        os._exit(97)

if __name__ == "__main__":
    print(json.dumps(service_permissions()))
