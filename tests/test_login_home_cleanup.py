from __future__ import annotations

import importlib.resources
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from rcp.agents import AgentLauncher
from rcp.machine_cards import LoginKey
from rcp.providers import profile_for
from rcp.runs.provider_login import (
    delete_removed_credential,
    login_homes_to_prepare,
    remove_shared_login,
)

from .test_provider_login_resolver import _login
from .test_provider_login_resolver import accounts as accounts


def _login_home(home: Path, shared: Path) -> dict[str, Path]:
    """A linked home beside its retained folders, with every kind of entry the walk meets."""
    (shared / "sessions").mkdir(parents=True)
    (shared / "sessions" / "rollout.jsonl").write_text("shared rollout")
    (shared / "config.toml").write_text("shared config")
    (home / "nested" / "empty").mkdir(parents=True)
    (home / "sessions").symlink_to(shared / "sessions")
    (home / "nested" / "config.toml").symlink_to(shared / "config.toml")
    (home / "auth.json").write_text("credential")
    (home / "private.txt").write_text("private")
    retained = home.with_name(home.name + "-retained")
    (retained / "pending").mkdir(parents=True)
    (retained / "pending" / "rollout.jsonl").write_text("retained")
    (retained / str(uuid.uuid4())).mkdir()
    return {"retained": retained, "shared": shared}


def _assert_pruned(home: Path, made: dict[str, Path]) -> None:
    assert sorted(path.name for path in home.iterdir()) == ["private.txt"]
    assert (made["shared"] / "sessions" / "rollout.jsonl").read_text() == "shared rollout"
    assert (made["shared"] / "config.toml").read_text() == "shared config"
    assert (made["retained"] / "pending" / "rollout.jsonl").read_text() == "retained"
    assert len(list(made["retained"].iterdir())) == 2


def test_removing_a_login_prunes_its_home_without_following_links(accounts, tmp_path) -> None:
    key = _login(accounts, "codex")
    home = accounts.credentials.login_home("codex", key.login_id)
    made = _login_home(home, tmp_path / "machine-home")

    remove_shared_login(accounts.store, AgentLauncher(), key, cancel_sign_in=lambda _: None)

    _assert_pruned(home, made)


def test_an_emptied_login_home_is_removed(accounts, tmp_path) -> None:
    key = _login(accounts, "codex")
    home = accounts.credentials.login_home("codex", key.login_id)
    made = _login_home(home, tmp_path / "machine-home")
    (home / "private.txt").unlink()

    remove_shared_login(accounts.store, AgentLauncher(), key, cancel_sign_in=lambda _: None)

    assert not home.exists()
    assert (made["retained"] / "pending" / "rollout.jsonl").read_text() == "retained"


def test_the_remote_helper_prunes_the_login_home(tmp_path, monkeypatch) -> None:
    login_id = str(uuid.uuid4())
    home = tmp_path / ".rcp" / "provider-homes" / "codex" / login_id
    made = _login_home(home, tmp_path / ".codex")
    monkeypatch.setenv("HOME", str(tmp_path))
    source = importlib.resources.files("rcp.server_ops").joinpath("provider_cleanup.py").read_text()

    subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            source,
            login_id,
            *profile_for("codex").remote_login_credential_files,
        ],
        check=True,
    )

    _assert_pruned(home, made)


@pytest.mark.parametrize("finished", [False, True])
def test_startup_prepares_a_removed_login_only_while_cleanup_is_pending(accounts, finished) -> None:
    store = accounts.store
    remote = store.create_space_machine(name="remote", host="remote", os_account="").machine_id
    active, removed = (
        LoginKey("codex", remote, store.add_provider_login("codex", remote, added_by="m").login_id)
        for _ in range(2)
    )
    store.finish_provider_login_removal(removed)
    delete_removed_credential(store, removed, defer=True)
    if finished:
        with store.connection() as connection:
            connection.execute(
                "UPDATE provider_login_states SET detail='Sign-in removed.' WHERE login_id=?",
                (removed.login_id,),
            )

    expected = {active} if finished else {active, removed}
    assert set(login_homes_to_prepare(store, "codex")) == expected
