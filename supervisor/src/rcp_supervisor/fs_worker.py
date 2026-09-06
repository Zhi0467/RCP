"""Service-account filesystem operations, called by the narrow root coordinator."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

from rcp_supervisor.checkpoint import (
    Checkpoint,
    SnapshotRoot,
    _directory,
    create_checkpoint,
    create_offline_snapshot,
    restore_checkpoint,
)
from rcp_supervisor.errors import SupervisorError
from rcp_supervisor.limits import MAX_CHECKPOINT_MANIFEST_BYTES


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    try:
        payload = sys.stdin.buffer.read(MAX_CHECKPOINT_MANIFEST_BYTES + 1)
        if len(payload) > MAX_CHECKPOINT_MANIFEST_BYTES:
            raise SupervisorError("Filesystem request exceeds its limit.")
        request = json.loads(payload)
        if (
            arguments == ["checkpoint"]
            and isinstance(request, dict)
            and request.keys() == {"directory", "roots", "boundary_sha256"}
        ):
            roots = tuple(
                SnapshotRoot(Path(item["live"]), Path(item["payload"])) for item in request["roots"]
            )
            result = asdict(
                create_checkpoint(
                    Path(request["directory"]), roots, boundary_sha256=request["boundary_sha256"]
                )
            )
            result["directory"] = str(result["directory"])
        elif (
            arguments == ["prepare-deployment-lock"]
            and isinstance(request, dict)
            and request.keys() == {"directory"}
        ):
            from rcp_supervisor.migration import prepare_deployment_lock

            prepare_deployment_lock(Path(request["directory"]))
            result = {"status": "ready"}
        elif (
            arguments == ["retire-source-keys"]
            and isinstance(request, dict)
            and request.keys() == {"credentials"}
        ):
            from rcp_supervisor.migration import retire_source_keys

            retire_source_keys(Path(request["credentials"]))
            result = {"status": "retired"}
        elif (
            arguments == ["workspace"]
            and isinstance(request, dict)
            and request.keys() == {"directory"}
        ):
            directory = Path(request["directory"])
            _directory(directory.parent)
            directory.mkdir(mode=0o700)
            result = {"directory": str(directory)}
        elif (
            arguments == ["snapshot"]
            and isinstance(request, dict)
            and request.keys() == {"directory", "live", "boundary_sha256"}
        ):
            result = asdict(
                create_offline_snapshot(
                    Path(request["directory"]),
                    Path(request["live"]),
                    boundary_sha256=request["boundary_sha256"],
                )
            )
            result["directory"] = str(result["directory"])
        elif (
            arguments == ["install"]
            and isinstance(request, dict)
            and request.keys() == {"bundle", "releases_root"}
        ):
            from rcp_supervisor.install import install_release

            result = {
                "release_directory": str(
                    install_release(Path(request["bundle"]), Path(request["releases_root"]))
                )
            }
        elif (
            arguments == ["restore"]
            and isinstance(request, dict)
            and request.keys() == {"directory", "sha256", "boundary_sha256"}
        ):
            restore_checkpoint(
                Checkpoint(
                    Path(request["directory"]), request["sha256"], request["boundary_sha256"]
                )
            )
            result = {"version": 1, "status": "restored"}
        else:
            raise SupervisorError("Filesystem operation or request format is unsupported.")
        print(json.dumps(result), flush=True)
        return 0
    except (SupervisorError, OSError, ValueError, TypeError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
