from __future__ import annotations

import json
import stat

from fastapi.testclient import TestClient

from rcp.server_runtime import (
    SERVER_METADATA_SCHEMA_VERSION,
    ServerMetadata,
    published_server_metadata,
    read_server_metadata,
    remove_server_metadata,
)

from .helpers import create_named_app as create_app


def test_metadata_is_published_atomically_and_removed_by_its_owner(tmp_path, monkeypatch) -> None:
    metadata = ServerMetadata.create(
        tmp_path,
        host="127.0.0.1",
        port=8421,
        owner_kind="desktop",
    )
    replaced = []

    import rcp.server_runtime as runtime

    real_replace = runtime.os.replace

    def observed_replace(source, destination):
        replaced.append((source, destination))
        real_replace(source, destination)

    monkeypatch.setattr(runtime.os, "replace", observed_replace)

    with published_server_metadata(tmp_path, metadata):
        assert read_server_metadata(tmp_path) == metadata
        metadata_file = tmp_path / "rcp-server.json"
        payload = json.loads(metadata_file.read_text(encoding="utf-8"))
        assert payload["schema_version"] == SERVER_METADATA_SCHEMA_VERSION
        assert stat.S_IMODE(metadata_file.stat().st_mode) == 0o600

    assert len(replaced) == 1
    assert replaced[0][0].name.startswith(".rcp-server.json.")
    assert replaced[0][1] == tmp_path / "rcp-server.json"
    assert not (tmp_path / "rcp-server.json").exists()


def test_metadata_cleanup_never_removes_a_replacement(tmp_path) -> None:
    original = ServerMetadata.create(
        tmp_path,
        host="127.0.0.1",
        port=8421,
        owner_kind="desktop",
    )
    replacement = ServerMetadata.create(
        tmp_path,
        host="127.0.0.1",
        port=18421,
        owner_kind="cli",
    )

    with published_server_metadata(tmp_path, original):
        (tmp_path / "rcp-server.json").write_text(
            json.dumps(replacement.as_dict()), encoding="utf-8"
        )

        assert remove_server_metadata(tmp_path, instance_id=original.instance_id) is False
        assert read_server_metadata(tmp_path) == replacement
        assert remove_server_metadata(tmp_path, instance_id=replacement.instance_id) is True


def test_frozen_app_shutdown_cleans_metadata_before_the_outer_server_context_exits(
    tmp_path, monkeypatch
) -> None:
    import rcp.api.app as app_module

    monkeypatch.setattr(app_module.sys, "frozen", True, raising=False)
    data_dir = tmp_path / "data"
    metadata = ServerMetadata.create(
        data_dir,
        host="127.0.0.1",
        port=18421,
        owner_kind="desktop",
    )

    with published_server_metadata(data_dir, metadata):
        app = create_app(data_dir=data_dir, instance_metadata=metadata)
        with TestClient(app):
            assert read_server_metadata(data_dir) == metadata
        assert not (data_dir / "rcp-server.json").exists()


def test_source_app_shutdown_leaves_outer_supervisor_metadata_in_place(tmp_path) -> None:
    data_dir = tmp_path / "data"
    metadata = ServerMetadata.create(
        data_dir,
        host="127.0.0.1",
        port=18421,
        owner_kind="cli",
    )

    with published_server_metadata(data_dir, metadata):
        app = create_app(data_dir=data_dir, instance_metadata=metadata)
        with TestClient(app):
            pass
        assert read_server_metadata(data_dir) == metadata


def test_remote_bundle_runs_on_a_bare_interpreter_and_registers_every_provider() -> None:
    """Execution hosts have no `rcp` package; `-I -S` hides it here too."""
    import subprocess
    import sys

    from rcp.providers import PROVIDERS, remote_bundle

    driver = "import json; print(json.dumps([sorted(SESSION_FORMATS), sorted(TURN_FENCES)]))"
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", remote_bundle(driver)],
        capture_output=True,
        text=True,
        check=True,
    )
    sources, runtimes = json.loads(result.stdout)
    indexed = {provider for provider, profile in PROVIDERS.items() if profile.session_format}
    assert sources == sorted({*indexed, "app_chat"})
    assert runtimes == sorted(
        runtime_id for profile in PROVIDERS.values() for runtime_id in profile.turn_fences
    )
