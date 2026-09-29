from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.agents import AgentLauncher
from rcp.providers import profile_for
from rcp.transport import provider_discovery
from rcp.transport.state import _remote_script

pytestmark = pytest.mark.real_provider_discovery


@pytest.fixture
def account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "execution account"
    home.mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    monkeypatch.setenv("HOME", str(tmp_path / "wrong-home"))
    monkeypatch.setattr(
        provider_discovery.pwd, "getpwuid", lambda _: SimpleNamespace(pw_dir=str(home))
    )
    return home


def executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "if sys.argv[1:] == ['--version']: print('1.18.33')\n"
        "elif sys.argv[1:] == ['models', '--verbose']: print('opencode/test\\n{}')\n"
    )
    path.chmod(0o755)
    return path


def discover() -> str | None:
    return provider_discovery.discover_provider("opencode", profile_for("opencode").install_paths)


@pytest.mark.parametrize("host", ["", "execution.example"])
def test_native_install_is_ready_without_path_or_symlink(
    account: Path, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    binary = executable(account / ".opencode/bin/opencode")
    if host:
        # Execute the shipped program and real stub CLI, replacing only the SSH
        # transport and OS account database so no human account is touched.
        def probe(_host: str, command: list[str], **_kwargs):
            if command[:2] == ["python3", "-c"]:
                source = command[2]
                prelude = (
                    "import pwd, types; "
                    f"pwd.getpwuid = lambda _: types.SimpleNamespace(pw_dir={str(account)!r}); "
                    f"exec(compile({source!r}, '<remote>', 'exec'))"
                )
                command = [sys.executable, "-c", prelude, *command[3:]]
            return subprocess.run(command, capture_output=True, text=True, check=False)

        monkeypatch.setattr(AgentLauncher, "_probe", staticmethod(probe))
    readiness = AgentLauncher().readiness("opencode", host=host)
    assert readiness.installed and readiness.authenticated
    assert readiness.binary_path == str(binary)
    assert readiness.version == "1.18.33"
    assert [model.id for model in readiness.models] == ["opencode/test"]


def test_discovery_precedence_and_stable_symlink(
    account: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = executable(account / ".opencode/bin/opencode")
    local = account / ".local/bin/opencode"
    local.parent.mkdir(parents=True)
    local.symlink_to(native)
    assert discover() == str(local)
    path_binary = executable(account / "path-bin/opencode")
    monkeypatch.setenv("PATH", str(path_binary.parent))
    assert discover() == str(path_binary)
    path_binary.unlink()
    assert discover() == str(local)
    local.unlink()
    assert discover() == str(native)


@pytest.mark.parametrize("kind", ["missing", "directory", "nonexecutable", "dangling"])
def test_invalid_native_install_is_not_discovered(account: Path, kind: str) -> None:
    candidate = account / ".opencode/bin/opencode"
    candidate.parent.mkdir(parents=True)
    if kind == "directory":
        candidate.mkdir()
    elif kind == "nonexecutable":
        candidate.write_text("not executable")
        candidate.chmod(0o644)
    elif kind == "dangling":
        candidate.symlink_to(account / "missing")
    assert discover() is None


def test_invalid_configured_path_does_not_fall_back(account: Path) -> None:
    executable(account / ".opencode/bin/opencode")
    configured = str(account / "missing-configured-opencode")
    readiness = AgentLauncher().readiness("opencode", binary=configured)
    assert not readiness.installed
    assert readiness.binary_path == configured
    assert readiness.path_state == "missing"


def test_shipped_discovery_uses_path_in_a_real_subprocess(
    account: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = executable(account / "path-bin/opencode")
    monkeypatch.setenv("PATH", str(binary.parent))
    result = subprocess.run(
        [sys.executable, "-c", _remote_script("provider_discovery.py"), "opencode"],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == str(binary)
