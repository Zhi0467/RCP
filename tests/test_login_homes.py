"""Overlay homes isolate credentials while sharing the execution account's state."""

from __future__ import annotations

import fcntl
from pathlib import Path

import pytest

from rcp.providers.login_homes import HomeLayout, home_lease, prepare_home

TOKEN = ".gemini/antigravity-cli/antigravity-oauth-token"
LAYOUT = HomeLayout(
    independent_paths=(TOKEN,),
    shared_directories=(
        ".gemini/antigravity-cli/conversations",
        ".gemini/antigravity-cli/brain",
        ".config",
    ),
)


@pytest.mark.parametrize(
    "relative", [".rcp/provider-homes/agy/login", "Library/App/RCP/provider-homes/agy/login"]
)
def test_overlay_ancestors_are_real_folders_without_return_branch(tmp_path: Path, relative: str):
    machine = tmp_path / "machine"
    home = machine / relative
    (machine / "Documents").mkdir(parents=True)
    (machine / TOKEN).parent.mkdir(parents=True)
    (machine / TOKEN).write_text("machine credential")
    prepare_home(home, machine, LAYOUT)
    assert not (home / TOKEN).exists()
    (home / TOKEN).write_text("added credential")
    assert (machine / TOKEN).read_text() == "machine credential"
    assert (home / "Documents").readlink() == machine / "Documents"
    for ancestor in Path(relative).parents:
        if ancestor == Path("."):
            continue
        assert (home / ancestor).is_dir()
        assert not (home / ancestor).is_symlink()
    assert not (home / relative).exists()
    for name in LAYOUT.shared_directories:
        assert (home / name).is_symlink()
        assert (machine / name).is_dir()


def test_private_and_atomically_replaced_names_migrate(tmp_path: Path):
    machine, home = tmp_path / "machine", tmp_path / "added"
    machine.mkdir()
    (machine / ".settings").write_text("old")
    prepare_home(home, machine, LAYOUT)
    (home / ".new").write_text("new")
    temporary = home / ".replacement"
    temporary.write_text("changed")
    temporary.replace(home / ".settings")
    prepare_home(home, machine, LAYOUT)
    for name, expected in ((".new", "new"), (".settings", "changed")):
        assert (home / name).is_symlink()
        assert (machine / name).read_text() == expected


def test_live_lease_defers_private_migration(tmp_path: Path):
    machine, home = tmp_path / "machine", tmp_path / "added"
    prepare_home(home, machine, LAYOUT)
    (home / ".new").write_text("live")
    with home_lease(home) as (lease, _):
        fcntl.flock(lease.fileno(), fcntl.LOCK_SH)
        prepare_home(home, machine, LAYOUT)
        assert not (home / ".new").is_symlink()
        assert not (machine / ".new").exists()
    prepare_home(home, machine, LAYOUT)
    assert (home / ".new").is_symlink()
    assert (machine / ".new").read_text() == "live"


def test_sibling_overlays_cannot_link_back_to_each_other(tmp_path: Path):
    machine = tmp_path / "machine"
    parent = Path(".rcp/provider-homes/agy")
    first, second = machine / parent / "first", machine / parent / "second"
    for home in (first, second, first):
        prepare_home(home, machine, LAYOUT)
    for home in (first, second):
        assert (home / parent).is_dir()
        assert not list((home / parent).iterdir())


def test_resolved_machine_home_excludes_overlay_through_an_alias(tmp_path: Path):
    machine = tmp_path / "machine"
    machine.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(machine, target_is_directory=True)
    relative = Path("Library/App/provider-homes/agy/added")
    home = alias / relative
    prepare_home(home, machine, LAYOUT)
    assert not (home / relative).exists()
    assert not (home / "Library").is_symlink()


def test_codex_shipped_source_runs_without_the_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import os
    import subprocess
    import sys
    from uuid import uuid4

    from rcp.agents.provider_environment import ProviderCredentialStore
    from rcp.providers.codex.auth import CodexAuthentication

    with monkeypatch.context() as transport:
        transport.setattr(
            subprocess,
            "run",
            lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", ""),
        )
        environment = CodexAuthentication().process_environment(
            ProviderCredentialStore(tmp_path / "providers"),
            "remote",
            str(uuid4()),
            host="unused-host",
        )
    source = environment.command_prefix[2]
    home, machine = tmp_path / "overlay", tmp_path / "machine"
    variables = dict(os.environ, CODEX_HOME=str(machine))
    prepared = subprocess.run(
        [sys.executable, "-I", "-c", source, str(home)],
        env=variables,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert prepared.returncode == 0, prepared.stderr
    variables.update(CODEX_HOME=str(home), RCP_CODEX_MACHINE_HOME=str(machine))
    child = """
import fcntl, os
from pathlib import Path
home = Path(os.environ['CODEX_HOME'])
with home.with_name(home.name + '.use.lock').open('a') as lease:
    try:
        fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('leased')
    else:
        raise SystemExit(1)
"""
    ran = subprocess.run(
        [sys.executable, "-I", "-c", source, "--run", str(home), sys.executable, "-I", "-c", child],
        env=variables,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.strip() == "leased"


def test_preexisting_sibling_link_is_removed_after_sibling_preparation(tmp_path: Path):
    machine = tmp_path / "machine"
    parent = Path(".rcp/provider-homes/agy")
    first, second = machine / parent / "first", machine / parent / "second"
    second.mkdir(parents=True)
    prepare_home(first, machine, LAYOUT)
    assert (first / parent / "second").is_symlink()
    prepare_home(second, machine, LAYOUT)
    prepare_home(first, machine, LAYOUT)
    for home in (first, second):
        assert not list((home / parent).iterdir())
