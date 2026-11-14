from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from rcp.agents.provider_environment import ProviderCredentialStore
from rcp.providers.agy.auth import AgyAuthentication
from rcp.providers.agy.profile import AgyProfile

TOKEN = ".gemini/antigravity-cli/antigravity-oauth-token"


@pytest.fixture
def login_environment(tmp_path, monkeypatch):
    machine = tmp_path / "machine"
    machine.mkdir()
    monkeypatch.setenv("HOME", str(machine))
    credentials = ProviderCredentialStore.for_data_dir(machine / "Library" / "RCP")
    return machine, credentials, str(uuid4())


def test_paste_driver_extracts_google_url_token():
    driver = AgyAuthentication().device_login()
    assert driver.accepts_code
    assert driver.command("fake-agy") == ["fake-agy", "acp", "--login"]
    url = "https://accounts.google.com/o/oauth2/auth?client_id=fake&state=one"
    assert driver.receive_line("Open " + url + " to sign in").fields == {"verification_url": url}
    assert driver.receive_line("https://accounts.google.com.evil.test/auth").fields == {}
    assert driver.receive_line("https://example.test/").fields == {}
    assert driver.code_input("private-code") == b"private-code\n"


@pytest.mark.parametrize("host", ["", "fake-host"])
def test_added_login_sets_home_only_at_exec(login_environment, host):
    machine, credentials, login_id = login_environment
    environment = AgyAuthentication().process_environment(credentials, "local", login_id, host=host)
    assert environment.local_env is None
    assert environment.remote_prefix is None
    assert environment.command_prefix.count("--agy-run" if host else "_agy-run") == 1
    code = 'import os,json; print(json.dumps({k:os.environ[k] for k in ("HOME","BROWSER")}))'
    result = subprocess.run(
        [*environment.command_prefix, sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    value = json.loads(result.stdout)
    expected = (
        machine / ".rcp/provider-homes/agy" / login_id
        if host
        else credentials.login_home("agy", login_id)
    )
    assert value["HOME"] == str(expected)
    assert value["BROWSER"].startswith(str(machine / ".rcp"))
    assert Path(value["BROWSER"]).read_text() == "#!/bin/sh\nexit 1\n"
    home = Path(value["HOME"])
    assert (home / ".gemini/antigravity-cli/conversations").is_symlink()
    assert (home / ".gemini/antigravity-cli/brain").is_symlink()
    assert not (home / TOKEN).exists()
    assert os.environ["HOME"] == str(machine)


def test_machine_login_has_no_overlay(login_environment):
    machine, credentials, _ = login_environment
    environment = AgyAuthentication().process_environment(credentials, "local", "", host="")
    assert "--home" not in environment.command_prefix
    assert not (credentials.root.parent / "provider-homes").exists()
    assert os.environ["HOME"] == str(machine)


def test_sign_out_and_cleanup_metadata(login_environment):
    machine, credentials, login_id = login_environment
    home = credentials.login_home("agy", login_id)
    token = home / TOKEN
    token.parent.mkdir(parents=True)
    token.write_text("added-secret")
    machine_token = machine / TOKEN
    machine_token.parent.mkdir(parents=True)
    machine_token.write_text("machine-secret")
    auth = AgyAuthentication()
    assert auth.supports_sign_out
    assert auth.sign_out(credentials, "local", login_id, "unused", host="") is None
    assert not token.exists()
    assert machine_token.read_text() == "machine-secret"
    profile = AgyProfile()
    assert profile.login_credential_files
    assert profile.login_home_credentials == (TOKEN,)
    assert profile.remote_login_credential_files == (
        f".rcp/provider-homes/agy/{{login_id}}/{TOKEN}",
    )


@pytest.mark.parametrize("added", [True, False])
def test_remote_sign_out_uses_execution_host_home(login_environment, monkeypatch, added):
    import shlex

    machine, credentials, login_id = login_environment
    if not added:
        login_id = ""
    home = machine / ".rcp/provider-homes/agy" / login_id if added else machine
    token = home / TOKEN
    token.parent.mkdir(parents=True)
    token.write_text("fake-token")
    monkeypatch.setattr(
        "rcp.transport.ssh.ssh_arguments",
        lambda host, command: [sys.executable, *shlex.split(command)[1:]],
    )
    assert (
        AgyAuthentication().sign_out(credentials, "local", login_id, "unused", host="fake-host")
        is None
    )
    assert not token.exists()
