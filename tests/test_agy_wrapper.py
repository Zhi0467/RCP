from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from rcp.agents.provider_environment import ProviderCredentialStore
from rcp.providers.agy.auth import AgyAuthentication
from rcp.providers.agy.remote import agy_run


class Executed(Exception):
    pass


@pytest.fixture
def execution(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)
    captured = {}

    def capture(binary, argv, environment):
        captured.update(
            binary=binary, argv=list(argv), environment=dict(environment), cwd=Path.cwd()
        )
        raise Executed

    monkeypatch.setattr(os, "execvpe", capture)
    return home, workspace, captured


def arguments():
    policy = {"permissions": {"allow": ["read_file(*)"], "ask": [], "deny": []}}
    return [
        "/fake/agy",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--rcp-agy-tools",
        '["view_file"]',
        "--agent",
        "rcp-preliminary",
        "--settings",
        json.dumps(policy),
    ]


def test_wrapper_execs_exact_binary_and_keeps_shims_stable(execution):
    home, workspace, captured = execution
    with pytest.raises(Executed):
        agy_run(["--", *arguments()])
    assert captured["binary"] == "/fake/agy"
    assert captured["argv"][0] == "/fake/agy"
    assert "--rcp-agy-tools" not in captured["argv"]
    directory = home / ".rcp" / "agy-browser-shims"
    assert directory.stat().st_mode & 0o777 == 0o700
    mtimes = {}
    for name in ("open", "xdg-open", "sensible-browser"):
        path = directory / name
        assert path.read_bytes() == b"#!/bin/sh\nexit 1\n"
        assert path.stat().st_mode & 0o777 == 0o700
        mtimes[name] = path.stat().st_mtime_ns
    assert captured["environment"]["BROWSER"] == str(directory / "open")
    assert captured["environment"]["PATH"].split(os.pathsep)[0] == str(directory)
    with pytest.raises(Executed):
        agy_run(["--", *arguments()])
    assert {name: (directory / name).stat().st_mtime_ns for name in mtimes} == mtimes
    argv = captured["argv"]
    agent = workspace / ".agents" / "agents" / (argv[argv.index("--agent") + 1] + ".md")
    assert agent.is_file()


def test_wrapper_denies_local_and_remote_provider_homes_without_hidden_scope(execution, tmp_path):
    home, _, captured = execution
    data = tmp_path / "data"
    with pytest.raises(Executed):
        agy_run(["--data-dir", str(data), "--", *arguments()])
    argv = captured["argv"]
    policy = json.loads(argv[argv.index("--settings") + 1])
    assert {
        f"read_file({data}/provider-homes)",
        f"read_file({home}/.rcp/provider-homes)",
        f"write_file({home}/.rcp/agy-browser-shims)",
    } <= set(policy["permissions"]["deny"])


def test_probe_uses_separate_workspace_and_strips_private_option(execution):
    _, workspace, captured = execution
    with pytest.raises(Executed):
        agy_run(["--", *arguments(), "--rcp-agy-probe"])
    assert captured["cwd"] != workspace
    assert "--rcp-agy-probe" not in captured["argv"]
    assert not (workspace / ".agents").exists()


@pytest.mark.parametrize("frozen", [False, True])
def test_local_wrapper_uses_rcp_runtime_and_optional_data_dir(tmp_path, monkeypatch, frozen):
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    authentication = AgyAuthentication()
    expected = [sys.executable, *([] if frozen else ["-m", "rcp"]), "_agy-run"]
    assert list(authentication.unmanaged_environment("").command_prefix) == [*expected, "--"]
    credentials = ProviderCredentialStore.for_data_dir(tmp_path / "data")
    assert list(
        authentication.process_environment(credentials, "backend", "", host="").command_prefix
    ) == [*expected, "--data-dir", str(tmp_path / "data"), "--"]
    remote = list(
        authentication.process_environment(credentials, "remote", "", host="example").command_prefix
    )
    assert remote[:2] == ["python3", "-c"]
    assert remote[-2:] == ["--agy-run", "--"]
    assert str(tmp_path) not in remote


def test_hidden_cli_dispatches_wrapper(monkeypatch):
    from rcp import __main__
    from rcp.providers.agy import remote

    captured = []
    monkeypatch.setattr(sys, "argv", ["rcp", "_agy-run", "--", "/fake/agy", "--version"])
    monkeypatch.setattr(remote, "agy_run", lambda arguments: captured.extend(arguments))
    __main__.main()
    assert captured == ["--", "/fake/agy", "--version"]


def test_local_wrapper_preserves_provider_pid(tmp_path):
    import subprocess

    home = tmp_path / "home"
    home.mkdir()
    fake = tmp_path / "fake-provider"
    fake.write_text(
        '#!/usr/bin/env python3\nimport json,os\nprint(json.dumps({"pid": os.getpid(), "browser": os.environ["BROWSER"]}))\n'
    )
    fake.chmod(0o700)
    prefix = AgyAuthentication().unmanaged_environment("").command_prefix
    with subprocess.Popen(
        [*prefix, str(fake), "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "HOME": str(home)},
    ) as process:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        value = json.loads(stdout)
        assert value["pid"] == process.pid
        assert value["browser"] == str(home / ".rcp" / "agy-browser-shims" / "open")
