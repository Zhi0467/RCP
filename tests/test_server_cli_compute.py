from __future__ import annotations

import os
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from fastapi.testclient import TestClient

from rcp.__main__ import build_parser
from rcp.api.app import create_app
from rcp.compute_jobs.models import ComputeBackendProbe
from rcp.server_ops.cli import CallerIdentity, run_server_command
from rcp.server_ops.compute import prepare_compute_probe_command
from rcp.server_ops.control import ServerControlClient, ServerControlComputeProbeResult
from rcp.server_runtime import ServerMetadata, published_server_metadata
from rcp.storage import AppStore, ProjectRecord
from tests.test_server_install import _temporary_layout

PROJECT_ID = "123e4567-e89b-42d3-b456-426614174001"


@pytest.mark.parametrize("ready", [True, False])
def test_server_compute_probe_runs_through_installed_service_and_stores_result(
    tmp_path, manifest, monkeypatch, ready
) -> None:
    import rcp.api.app as app_module

    layout = _temporary_layout(tmp_path)
    data_dir = layout.data_dir
    store, _ = AppStore.initialize_team_space(data_dir / "rcp.sqlite3", "Compute lab")
    store.upsert_project(
        ProjectRecord(
            project_id=PROJECT_ID,
            home_space_id=store.space_id,
            name=manifest.name,
            locator=str(manifest.path),
            added_at=store.now(),
            state_location=str(manifest.path.parent),
            state_remote=False,
        )
    )
    result = ComputeBackendProbe(
        execution_machine="laptop",
        backend_id="systemd_user",
        state="ready" if ready else "failed",
        ready=ready,
        containment="mirrored" if ready else "cooperative",
        diagnostic="Probe job exited zero." if ready else "User manager unavailable.",
        required_action=None if ready else "Enable the user manager and probe again.",
        status_label="Ready" if ready else "Failed",
        status_tone="ready" if ready else "error",
    )
    calls = []

    def probe(loaded, machine_alias, *, data_dir):
        calls.append((loaded.path, machine_alias, data_dir))
        return result

    monkeypatch.setattr(app_module, "probe_compute_backend", probe)
    # A short private Unix socket path is required even when pytest's temp root is long.
    with TemporaryDirectory(prefix="rcp-compute-", dir="/tmp") as socket_dir:
        os.chown(socket_dir, os.geteuid(), os.getegid())
        metadata = ServerMetadata.create(
            data_dir,
            host="127.0.0.1",
            port=18423,
            owner_kind="cli",
            control_socket=Path(socket_dir) / "control.sock",
        )
        app = create_app(data_dir=data_dir, instance_metadata=metadata)
        args = build_parser().parse_args(
            ["server", "compute", "probe", "--project", PROJECT_ID, "laptop"]
        )
        output = StringIO()
        exchange = ServerControlClient._exchange
        envelopes = []

        def observe_exchange(client, request):
            envelope = exchange(client, request)
            if request.operation == "compute_backend_probe":
                envelopes.append(envelope)
            return envelope

        monkeypatch.setattr(ServerControlClient, "_exchange", observe_exchange)
        with published_server_metadata(data_dir, metadata), TestClient(app):
            # The CLI must use the running service's existing store.
            monkeypatch.setattr(
                AppStore, "__init__", lambda *_args, **_kwargs: pytest.fail("second AppStore")
            )
            code = run_server_command(
                args,
                identity=CallerIdentity(uid=501, username="rcp", host="lab"),
                handler=lambda request, identity: prepare_compute_probe_command(
                    request, identity, layout=layout
                ),
                stream=output,
            )
        assert code == (0 if ready else 1), output.getvalue()
        assert envelopes == [
            ServerControlComputeProbeResult(
                instance_id=metadata.instance_id,
                pid=metadata.pid,
                data_dir_id=metadata.data_dir_id,
                space_id=store.space_id,
                selector_kind="project",
                selector_id=PROJECT_ID,
                machine_alias="laptop",
                probe=result,
            )
        ]
    assert calls == [(manifest.path, "laptop", data_dir)]
    assert store.compute_backend_probe(PROJECT_ID, "laptop") == result
    for text in (result.status_label, result.backend_id, result.containment, result.diagnostic):
        assert text in output.getvalue()


def test_compute_probe_requires_the_service_account() -> None:
    output = StringIO()
    args = build_parser().parse_args(
        ["server", "compute", "probe", "--project", PROJECT_ID, "laptop"]
    )
    code = run_server_command(
        args,
        identity=CallerIdentity(uid=0, username="root", host="lab"),
        handler=lambda *_: pytest.fail("wrong account must not execute"),
        stream=output,
    )
    assert code == 77
    assert "rcp" in output.getvalue()


def test_compute_control_preserves_old_wire_shape_and_requires_new_protocol() -> None:
    import json
    import uuid

    from rcp.server_ops.control import ServerControlRequest

    fields = dict(request_id=str(uuid.uuid4()), instance_id=str(uuid.uuid4()))
    ordinary = ServerControlRequest(operation="probe", protocol_version=10, **fields)
    assert "machine_alias" not in json.loads(ordinary.model_dump_json())
    with pytest.raises(ValueError, match="project and machine alias"):
        ServerControlRequest(
            operation="compute_backend_probe",
            protocol_version=10,
            selector_kind="project",
            selector_id=PROJECT_ID,
            machine_alias="laptop",
            **fields,
        )
