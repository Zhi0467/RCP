"""Added Codex logins share configuration and one session store with the machine home."""

from __future__ import annotations

import stat
from pathlib import Path
from uuid import uuid4

import pytest

from rcp.agents.provider_environment import ProviderCredentialStore
from rcp.providers.codex.homes import MIGRATION_LOCK, SHARED_FILES, SHARED_SESSIONS, prepare_home
from rcp.providers.codex.homes import _legacy_home_idle as inspect_legacy_home
from rcp.storage.models import MACHINE_LOGIN_ID


@pytest.fixture(autouse=True)
def idle_legacy_homes(monkeypatch: pytest.MonkeyPatch) -> None:
    # These disposable fixture homes have no legacy Codex processes. The guard
    # itself is covered below; the sandbox can prohibit host process inspection.
    monkeypatch.setattr("rcp.providers.codex.homes._legacy_home_idle", lambda home: True)


def test_home_links_and_retains_regular_file_conflict(tmp_path: Path) -> None:
    machine = tmp_path / "machine"
    machine.mkdir()
    home = prepare_home(tmp_path / "added", machine)
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    shared = SHARED_FILES + SHARED_SESSIONS
    assert {name: (home / name).readlink() for name in shared} == {
        name: machine / name for name in shared
    }
    assert prepare_home(home, machine) == home
    config = home / "config.toml"
    config.unlink()
    config.write_text("owned by Codex")
    prepare_home(home, machine)
    assert config.is_symlink()
    retained = list(home.with_name(home.name + "-retained").glob("*/config.toml"))
    assert len(retained) == 1
    assert retained[0].read_text() == "owned by Codex"


def test_existing_sessions_move_to_the_machine_store(tmp_path: Path) -> None:
    machine = tmp_path / "machine"
    (machine / "sessions/2026/10/01").mkdir(parents=True)
    (machine / "sessions/2026/10/01/rollout-same.jsonl").write_text("same\n")
    (machine / "session_index.jsonl").write_text('{"id":"a"}\n')
    home = tmp_path / "added"
    (home / "sessions/2026/10/01").mkdir(parents=True)
    (home / "sessions/2026/10/01/rollout-own.jsonl").write_text("own\n")
    (home / "sessions/2026/10/01/rollout-same.jsonl").write_text("same\n")
    (home / "archived_sessions").mkdir()
    (home / "archived_sessions/rollout-old.jsonl").write_text("old\n")
    (home / "session_index.jsonl").write_text('{"id":"a"}\n{"id":"b"}\n')

    prepare_home(home, machine)
    prepare_home(home, machine)

    assert (home / "sessions/2026/10/01/rollout-own.jsonl").read_text() == "own\n"
    assert (machine / "sessions/2026/10/01/rollout-own.jsonl").read_text() == "own\n"
    assert (machine / "archived_sessions/rollout-old.jsonl").read_text() == "old\n"
    assert (machine / "session_index.jsonl").read_text().splitlines() == [
        '{"id":"a"}',
        '{"id":"b"}',
    ]
    assert all((home / name).is_symlink() for name in SHARED_SESSIONS)


def test_conflicting_session_file_is_never_overwritten(tmp_path: Path) -> None:
    machine = tmp_path / "machine"
    (machine / "sessions").mkdir(parents=True)
    (machine / "sessions/rollout-x.jsonl").write_text("machine\n")
    home = tmp_path / "added"
    (home / "sessions").mkdir(parents=True)
    (home / "sessions/rollout-x.jsonl").write_text("added\n")
    with pytest.raises(FileExistsError, match="rollout-x.jsonl"):
        prepare_home(home, machine)
    assert (machine / "sessions/rollout-x.jsonl").read_text() == "machine\n"
    assert (home / "sessions/rollout-x.jsonl").read_text() == "added\n"


@pytest.mark.parametrize(
    "login_id", ["../escape", "not-a-uuid", "00000000-0000-1000-8000-000000000000"]
)
def test_login_path_rejects_non_uuid4(tmp_path: Path, login_id: str) -> None:
    with pytest.raises(ValueError):
        ProviderCredentialStore(tmp_path).account_dir("codex", "", login_id)


def test_added_login_path_preserves_machine_path(tmp_path: Path) -> None:
    credentials = ProviderCredentialStore(tmp_path)
    login_id = str(uuid4())
    machine = credentials.account_dir("codex", "", login_id=MACHINE_LOGIN_ID)
    assert machine == tmp_path / "codex" / "local"
    assert credentials.account_dir("codex", "", login_id) == machine / "logins" / login_id


def test_added_codex_environment_and_credential_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rcp.providers.codex.auth import CodexAuthentication
    from rcp.storage.models import ProviderLoginStateRecord

    machine = tmp_path / "machine"
    monkeypatch.setenv("CODEX_HOME", str(machine))
    credentials = ProviderCredentialStore(tmp_path / "providers")
    login_id = str(uuid4())
    auth = CodexAuthentication()
    environment = auth.process_environment(credentials, "", login_id)
    assert environment.local_env is not None
    home = Path(environment.local_env["CODEX_HOME"])
    assert home == credentials.login_home("codex", login_id)
    assert (home / "skills").readlink() == machine / "skills"
    state = ProviderLoginStateRecord(provider="codex", host="", login_id=login_id)
    assert auth.credential_available(credentials, "", login_id, login_state=state)
    assert not auth.credential_available(
        credentials, "", login_id, login_state=state.model_copy(update={"state": "signed_out"})
    )
    assert not auth.credential_available(credentials, "", login_id)
    assert auth.sign_out(credentials, "", login_id, "codex") == ["codex", "logout"]


@pytest.mark.parametrize("interrupt_after", ["state_5.sqlite", "state_5.sqlite-wal"])
@pytest.mark.parametrize("defer_retry", [False, True])
def test_interrupted_state_retention_reuses_snapshot(
    tmp_path, monkeypatch, interrupt_after, defer_retry
):
    home, machine = tmp_path / "added", tmp_path / "machine"
    home.mkdir()
    originals = {
        "state_5.sqlite": b"database bytes",
        "state_5.sqlite-wal": b"journal bytes",
        "state_5.sqlite-shm": b"shared memory bytes",
    }
    for name, content in originals.items():
        (home / name).write_bytes(content)
    rename = Path.rename

    def interrupted_rename(source, target):
        result = rename(source, target)
        if source.parent == home and source.name == interrupt_after:
            raise InterruptedError("simulated process interruption after move")
        return result

    with monkeypatch.context() as fault:
        fault.setattr(Path, "rename", interrupted_rename)
        with pytest.raises(InterruptedError):
            prepare_home(home, machine)
    if defer_retry:
        prepare_home(home, machine, migrate=False)
        assert (tmp_path / "added-retained/pending").is_dir()
    prepare_home(home, machine)
    prepare_home(home, machine)
    (snapshot,) = (tmp_path / "added-retained").iterdir()
    assert {path.name: path.read_bytes() for path in snapshot.iterdir()} == originals
    assert all((home / name).is_symlink() for name in originals)


@pytest.mark.parametrize(
    "name",
    ["state_5.sqlite", "state_5.sqlite-wal", "history.jsonl", "installation_id", "future_cache"],
)
def test_noncredential_state_is_retained_and_shared(tmp_path: Path, name: str) -> None:
    machine, home = tmp_path / "machine", tmp_path / "added"
    machine.mkdir()
    home.mkdir()
    (machine / name).write_bytes(b"machine")
    (home / name).write_bytes(b"old login")
    (home / "auth.json").write_bytes(b"credential")
    prepare_home(home, machine)
    prepare_home(home, machine)
    assert (home / name).read_bytes() == b"machine"
    assert (home / "auth.json").read_bytes() == b"credential"
    assert not (home / "auth.json").is_symlink()
    assert [p.read_bytes() for p in (tmp_path / "added-retained").glob("*/" + name)] == [
        b"old login"
    ]


def test_new_directory_and_later_machine_entries_are_shared(tmp_path: Path) -> None:
    home, machine = tmp_path / "added", tmp_path / "machine"
    (home / "future_directory").mkdir(parents=True)
    (home / "future_directory/item").write_text("retained")
    prepare_home(home, machine)
    assert (home / "future_directory").is_dir()
    assert (home / "future_directory").is_symlink()
    (machine / "new_shared").write_text("new")
    prepare_home(home, machine)
    assert (home / "new_shared").read_text() == "new"
    assert not (home / MIGRATION_LOCK).exists()


def test_live_home_lease_defers_migration(tmp_path: Path) -> None:
    import fcntl

    from rcp.providers.codex.homes import _home_lease

    machine, home = tmp_path / "machine", tmp_path / "added"
    prepare_home(home, machine)
    stray = home / "later.sqlite"
    stray.write_bytes(b"live")
    with _home_lease(home) as (lease, _):
        fcntl.flock(lease.fileno(), fcntl.LOCK_SH)
        prepare_home(home, machine)
        assert not stray.is_symlink()
        assert stray.read_bytes() == b"live"
    prepare_home(home, machine)
    assert stray.is_symlink()
    assert next((tmp_path / "added-retained").glob("*/later.sqlite")).read_bytes() == b"live"


def test_remote_home_source_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ast
    import shlex
    import subprocess

    from rcp.providers.codex.auth import CodexAuthentication

    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    login_id = str(uuid4())
    env = CodexAuthentication().process_environment(
        ProviderCredentialStore(tmp_path), "example-host", login_id
    )
    assert env.local_env is None
    assert env.remote_prefix is not None
    assert "${CODEX_HOME:-$HOME/.codex}" in env.remote_prefix
    assert env.command_prefix[:2] == ("python3", "-c")
    assert env.command_prefix[3:] == ("--run", f"~/.rcp/provider-homes/codex/{login_id}")
    remote_shell = shlex.split(calls[0][-1])
    remote_python = shlex.split(remote_shell[-1])
    assert remote_python[:2] == ["python3", "-c"]
    assert remote_python[-1] == f"~/.rcp/provider-homes/codex/{login_id}"
    ast.parse(remote_python[2], feature_version=(3, 8))
    assert remote_python[2] == env.command_prefix[2]


@pytest.mark.parametrize("frozen", [False, True])
def test_local_home_command_supports_frozen_backend(tmp_path, monkeypatch, frozen):
    import sys

    from rcp.providers.codex.auth import CodexAuthentication

    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "machine"))
    environment = CodexAuthentication().process_environment(
        ProviderCredentialStore(tmp_path / "providers"), "", str(uuid4())
    )
    assert environment.local_env is not None
    assert environment.command_prefix == (
        sys.executable,
        *(() if frozen else ("-m", "rcp")),
        "_codex-home-run",
        environment.local_env["CODEX_HOME"],
    )


def test_home_cli_dispatch_preserves_command_arguments(tmp_path, monkeypatch):
    import sys

    from rcp import __main__
    from rcp.providers.codex import homes

    calls = []
    monkeypatch.setattr(homes, "_run", lambda home, command: calls.append((home, command)))
    monkeypatch.setattr(
        sys, "argv", ["rcp", "_codex-home-run", str(tmp_path), "codex", "exec", "a b"]
    )
    __main__.main()
    assert calls == [(tmp_path, ["codex", "exec", "a b"])]


def test_home_command_keeps_lease_through_exec(tmp_path, monkeypatch):
    import subprocess
    import sys

    from rcp.providers.codex.auth import CodexAuthentication

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "machine"))
    environment = CodexAuthentication().process_environment(
        ProviderCredentialStore(tmp_path / "providers"), "", str(uuid4())
    )
    # The exec'ed process opens a second descriptor. An inherited shared lease
    # must prevent it from acquiring an exclusive migration lock.
    child = """
import fcntl, os
from pathlib import Path
home = Path(os.environ['CODEX_HOME'])
with home.with_name(home.name + '.use.lock').open('a') as lease:
    try:
        fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(0)
    raise SystemExit(1)
"""
    result = subprocess.run(
        [*environment.command_prefix, sys.executable, "-c", child],
        env=environment.local_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("running", [False, True])
def test_legacy_process_environment_defers_migration(tmp_path, monkeypatch, running):
    import subprocess

    home = tmp_path / "added home"
    output = (
        f"1234 renamed-provider CODEX_HOME={home} OTHER=value\n"
        if running
        else "1234 unrelated CODEX_HOME=/different/home\n"
    )
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, output, "")
    )
    assert inspect_legacy_home(home) is not running


def test_unavailable_process_inspection_defers_migration(tmp_path, monkeypatch):
    import subprocess

    def unavailable(*args, **kwargs):
        raise PermissionError("process inspection unavailable")

    monkeypatch.setattr(subprocess, "run", unavailable)
    assert not inspect_legacy_home(tmp_path / "added")


def test_process_inspector_does_not_inherit_login_home(tmp_path, monkeypatch):
    import subprocess

    home = tmp_path / "added"
    monkeypatch.setenv("CODEX_HOME", str(home))

    def run(*args, **kwargs):
        assert "CODEX_HOME" not in kwargs["env"]
        return subprocess.CompletedProcess(args, 0, "1234 ps OTHER=value\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert inspect_legacy_home(home)


def test_rollout_migration_across_filesystems(tmp_path, monkeypatch):
    import errno
    import os

    home, machine = tmp_path / "added", tmp_path / "machine"
    source = home / "sessions/rollout.jsonl"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"rollout\n")
    replace = os.replace

    def cross_device(old, new):
        if old == source:
            raise OSError(errno.EXDEV, "different devices")
        return replace(old, new)

    monkeypatch.setattr(os, "replace", cross_device)
    prepare_home(home, machine)
    assert source.read_bytes() == b"rollout\n"
    assert (machine / "sessions/rollout.jsonl").read_bytes() == b"rollout\n"
    assert (home / "sessions").is_symlink()
    assert not list((machine / "sessions").glob(".rcp-migration-*"))


def test_parallel_login_migrations_preserve_both_indexes(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    machine = tmp_path / "machine"
    homes = [tmp_path / "first", tmp_path / "second"]
    for index, home in enumerate(homes):
        (home / "sessions").mkdir(parents=True)
        (home / f"sessions/rollout-{index}.jsonl").write_text(str(index))
        (home / "session_index.jsonl").write_text(f'{{"id":"{index}"}}\n')
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda home: prepare_home(home, machine), homes))
    assert set((machine / "session_index.jsonl").read_text().splitlines()) == {
        '{"id":"0"}',
        '{"id":"1"}',
    }
    for home in homes:
        assert sorted(path.read_text() for path in (home / "sessions").iterdir()) == ["0", "1"]


def test_remote_home_diagnostic_is_visible_without_shell_output(tmp_path, monkeypatch, caplog):
    import logging
    import subprocess

    from rcp.providers.codex.auth import CodexAuthentication

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, "", "private-shell-output"
        ),
    )
    CodexAuthentication().process_environment(
        ProviderCredentialStore(tmp_path), "example-host", str(uuid4())
    )
    assert any(record.levelno == logging.WARNING for record in caplog.records)
    assert "private-shell-output" not in caplog.text


@pytest.mark.parametrize("live", [False, True])
def test_hidden_legacy_home_moves_beside_the_credential_namespace(tmp_path, monkeypatch, live):
    import fcntl

    from rcp.providers.codex.auth import CodexAuthentication
    from rcp.providers.codex.homes import _home_lease

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "machine"))
    credentials = ProviderCredentialStore(tmp_path / "providers")
    login_id = str(uuid4())
    # The 0.4.36 layout: the home inside the hidden `providers/` namespace.
    legacy = credentials.account_dir("codex", "", login_id) / "codex-home"
    (legacy / "sessions").mkdir(parents=True)
    (legacy / "sessions/rollout.jsonl").write_text("rollout\n")
    (legacy / "auth.json").write_text("credential")
    (legacy.with_name("codex-home-retained") / "snapshot").mkdir(parents=True)

    def home() -> Path:
        environment = CodexAuthentication().process_environment(credentials, "", login_id)
        assert environment.local_env is not None
        return Path(environment.local_env["CODEX_HOME"])

    if live:
        with _home_lease(legacy) as (lease, _):
            fcntl.flock(lease.fileno(), fcntl.LOCK_SH)
            assert home() == legacy
            assert not legacy.is_symlink()
    moved = credentials.login_home("codex", login_id)
    assert home() == home() == moved
    assert (moved / "auth.json").read_text() == "credential"
    assert (tmp_path / "machine/sessions/rollout.jsonl").read_text() == "rollout\n"
    assert (moved.with_name(moved.name + "-retained") / "snapshot").is_dir()
    # A rolled-back release finds the same home at its old path, new sign-ins too.
    fresh = str(uuid4())
    CodexAuthentication().process_environment(credentials, "", fresh)
    for login, path in ((login_id, moved), (fresh, credentials.login_home("codex", fresh))):
        assert (credentials.account_dir("codex", "", login) / "codex-home").readlink() == path
