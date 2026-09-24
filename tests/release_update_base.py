"""Capture representative team data and prepare its release update.

The old-data upgrade gate runs this with a recent release's code, then validates
the prepared update with the candidate, as a real server update does.
"""

from __future__ import annotations

import json
import os
import pwd
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rcp.storage.models as storage_models  # noqa: E402
from rcp.api import create_app  # noqa: E402
from rcp.server_ops.backup_capture import BackupCaptureCoordinator  # noqa: E402
from rcp.server_ops.deployment import PrepareRequest, prepare  # noqa: E402
from rcp.server_runtime import ServerMetadata  # noqa: E402
from rcp.storage import AppStore  # noqa: E402
from tests.supervisor_reboot_data import prepare_data  # noqa: E402


def main(root: Path) -> None:
    account = pwd.getpwuid(os.geteuid()).pw_name
    storage_models.DEFAULT_SERVER_LAYOUT = SimpleNamespace(
        service_account=account, projects_root=root / "projects"
    )
    data = root / "data"
    receipt = prepare_data(data, root / "projects", account=account)
    # A running server holds each project's display cache; the switched release reads it.
    app = create_app(
        data_dir=data, trusted_principal_resolver=lambda _request, _store: receipt["member_id"]
    )
    response = TestClient(app).get(f"/api/projects/{receipt['project_id']}")
    if response.status_code != 200 or not (data / "project-snapshots").is_dir():
        raise RuntimeError(f"the display cache was not written: {response.status_code}")
    (data / "run-stage").chmod(0o700)
    with tempfile.TemporaryDirectory(prefix="rcp-maint-", dir="/tmp") as sockets:
        metadata = ServerMetadata.create(
            data,
            host="127.0.0.1",
            port=8421,
            owner_kind="cli",
            control_socket=Path(sockets) / "control.sock",
            running_commit="a" * 40,
            web_build_id="sha256:" + "b" * 64,
        )
        capture = BackupCaptureCoordinator(
            AppStore(data / "rcp.sqlite3"), data, metadata
        ).capture_sqlite()
        prepared = prepare(
            PrepareRequest(
                version=1,
                data_dir=str(data),
                output_dir=str(root / "prepared"),
                sqlite_receipt_path=str(capture.receipt_path),
                sqlite_receipt_sha256=capture.receipt_sha256,
            )
        )
    print(json.dumps(prepared))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
