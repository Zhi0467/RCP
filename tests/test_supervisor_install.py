from __future__ import annotations

import json
import os
import subprocess
import venv
from pathlib import Path

import pytest
from rcp_supervisor import cli, install
from rcp_supervisor.errors import SupervisorError

from tests.supervisor_helpers import make_bundle, refresh_manifest


def test_install_real_wheel_in_isolated_environment_preserves_current_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = make_bundle(tmp_path / "bundle")
    root = tmp_path / "releases"
    root.mkdir(mode=0o700)
    previous = root / "previous"
    previous.mkdir()
    sentinel = previous / "sentinel"
    sentinel.write_text("old release")
    current = tmp_path / "current"
    current.symlink_to(previous, target_is_directory=True)
    # Ambient project/uv settings must not bypass hash checking or redirect the
    # isolated environment, even when launched from a development shell.
    monkeypatch.setenv("UV_NO_VERIFY_HASHES", "1")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(previous))
    monkeypatch.setenv("PYTHONPATH", str(previous))

    target = install.install_release(bundle, root)

    receipt = json.loads((target / "installed.json").read_text())
    result = subprocess.run(
        [str(target / ".venv/bin/python"), "-I", "-c", "import rcp; print(rcp.__version__)"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert result.stdout.strip() == receipt["version"]
    assert target.name == str(receipt["build"])
    assert current.resolve() == previous
    assert sentinel.read_text() == "old release"
    with pytest.raises(SupervisorError, match="already exists"):
        install.install_release(bundle, root)
    assert sentinel.read_text() == "old release"


def test_install_preserves_partial_failure_without_publishing_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = make_bundle(tmp_path / "bundle")
    root = tmp_path / "releases"
    root.mkdir(mode=0o700)

    def fail(*args, **kwargs):
        kwargs["log"].write(b"diagnostic fixture\n")
        raise SupervisorError("injected install failure")

    monkeypatch.setattr(install, "_run", fail)
    with pytest.raises(SupervisorError, match="retained"):
        install.install_release(bundle, root)
    (target,) = root.iterdir()
    assert not (target / "installed.json").exists()
    assert (target / "install.log").read_bytes() == b"diagnostic fixture\n"
    assert (target / "assets/manifest.sha256").exists()


def test_install_refuses_bad_hash_before_creating_release(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path / "bundle")
    (bundle / "requirements.lock.txt").write_text("changed")
    root = tmp_path / "releases"
    root.mkdir(mode=0o700)
    with pytest.raises(SupervisorError, match="SHA-256 mismatch"):
        install.install_release(bundle, root)
    assert not list(root.iterdir())


def test_install_binds_copied_assets_to_the_initial_verified_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = make_bundle(tmp_path / "bundle")
    root = tmp_path / "releases"
    root.mkdir(mode=0o700)
    original_verify = install.verify_release

    def replace_after_verification(path: Path):
        verified = original_verify(path)
        if path == bundle:
            assets = {entry.name: entry.read_bytes() for entry in bundle.iterdir()}
            assets["requirements.lock.txt"] += b"# changed after initial verification\n"
            refresh_manifest(assets)
            for name, data in assets.items():
                (bundle / name).write_bytes(data)
        return verified

    monkeypatch.setattr(install, "verify_release", replace_after_verification)
    with pytest.raises(SupervisorError, match="changed while copying"):
        install.install_release(bundle, root)
    (target,) = root.iterdir()
    assert not (target / ".venv").exists()
    assert not (target / "installed.json").exists()


def test_install_refuses_root_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    with pytest.raises(SupervisorError, match="not root"):
        install.install_release(tmp_path / "bundle", tmp_path / "releases")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("unsafe", ["symlink", "writable"])
def test_install_refuses_unsafe_destination(tmp_path: Path, unsafe: str) -> None:
    root = tmp_path / "releases"
    root.mkdir(mode=0o700)
    destination = root
    if unsafe == "symlink":
        destination = tmp_path / "link"
        destination.symlink_to(root, target_is_directory=True)
    else:
        root.chmod(0o777)
    with pytest.raises(SupervisorError, match="symbolic link|writable"):
        install.install_release(tmp_path / "missing-bundle", destination)


def test_cli_verify_emits_terminal_success_or_failure(tmp_path: Path, capsys) -> None:
    bundle = make_bundle(tmp_path / "bundle")
    assert cli.main(["--machine-readable", "verify", str(bundle)]) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [event["step"]["state"] for event in events] == ["running", "succeeded"]
    assert all(
        event["version"] == 1 and event["command"] == "supervisor verify" for event in events
    )

    (bundle / "requirements.lock.txt").write_text("tampered")
    assert cli.main(["--machine-readable", "verify", str(bundle)]) == 1
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[-1]["step"]["state"] == "failed"
    assert "SHA-256 mismatch" in events[-1]["step"]["message"]


@pytest.mark.parametrize("mode", [0o777, 0o1777])
def test_require_directory_checks_shared_ancestor_permissions(tmp_path: Path, mode: int) -> None:
    parent = tmp_path / "shared"
    parent.mkdir()
    parent.chmod(mode)
    root = parent / "releases"
    root.mkdir(mode=0o700)
    if mode == 0o777:
        with pytest.raises(SupervisorError, match="ancestors must not be writable"):
            install._require_directory(root)
    else:
        install._require_directory(root)


@pytest.mark.parametrize("failed_directory", ["target", "root"])
def test_install_removes_receipt_after_publish_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_directory: str
) -> None:
    bundle = make_bundle(tmp_path / "bundle")
    root = tmp_path / "releases"
    root.mkdir(mode=0o700)
    managed_python = tmp_path / "cpython-3.12-fake/bin/python3"
    managed_python.parent.mkdir(parents=True)
    managed_python.touch()
    synced = []

    def run(*args, cwd, log, **kwargs):
        log.write(b"successful install fixture\n")
        python = cwd / ".venv/bin/python"
        if not python.exists():
            python.parent.mkdir(parents=True)
            python.symlink_to(managed_python)

    def fail_fsync(path):
        assert (root / "412/installed.json").is_file()
        synced.append(path)
        if path == (root if failed_directory == "root" else root / "412"):
            raise OSError("EIO")

    monkeypatch.setattr(install, "_run", run)
    monkeypatch.setattr(install, "_verify_installed_identity", lambda *args, **kwargs: None)
    monkeypatch.setattr(install, "_protect_venv_lock", lambda *args: None)
    monkeypatch.setattr(install, "_fsync_owned_tree", lambda *args: None)
    monkeypatch.setattr(install, "_fsync_directory", fail_fsync)
    with pytest.raises(SupervisorError, match="retained.*EIO"):
        install.install_release(bundle, root)
    (target,) = root.iterdir()
    assert synced == ([target, root] if failed_directory == "root" else [target])
    assert not (target / "installed.json").exists()
    assert not (target / ".installed.json.tmp").exists()
    assert b"successful install fixture" in (target / "install.log").read_bytes()


def test_verify_installed_identity_retains_import_stderr(tmp_path: Path) -> None:
    release = install.verify_release(make_bundle(tmp_path / "bundle"))
    environment = tmp_path / "environment"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
    (site_packages,) = environment.glob("lib/python*/site-packages")
    package = site_packages / "rcp"
    package.mkdir()
    package.joinpath("__init__.py").write_text('raise ImportError("broken identity fixture")\n')
    log_path = tmp_path / "identity.log"
    with log_path.open("wb") as log:
        log.write(b"prior install diagnostic\n")
        with pytest.raises(SupervisorError, match="version does not match"):
            install._verify_installed_identity(
                release, environment / "bin/python", cwd=tmp_path, log=log
            )
    diagnostic = log_path.read_text()
    assert diagnostic.startswith("prior install diagnostic\n")
    assert "ImportError: broken identity fixture" in diagnostic
