"""Added Codex logins share configuration and one session store with the machine home."""

from __future__ import annotations

import stat
from pathlib import Path
from uuid import uuid4

import pytest

from rcp.agents.provider_environment import ProviderCredentialStore
from rcp.providers.codex.homes import SHARED_FILES, SHARED_SESSIONS, prepare_home
from rcp.storage.models import MACHINE_LOGIN_ID


def test_home_links_and_regular_file_conflict(tmp_path: Path) -> None:
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
    with pytest.raises(FileExistsError, match="config.toml"):
        prepare_home(home, machine)
    assert config.read_text() == "owned by Codex"
    assert not config.is_symlink()


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
    assert home == credentials.account_dir("codex", "", login_id) / "codex-home"
    assert (home / "skills").readlink() == machine / "skills"
    state = ProviderLoginStateRecord(provider="codex", host="", login_id=login_id)
    assert auth.credential_available(credentials, "", login_id, login_state=state)
    assert not auth.credential_available(
        credentials, "", login_id, login_state=state.model_copy(update={"state": "signed_out"})
    )
    assert not auth.credential_available(credentials, "", login_id)
    assert auth.sign_out(credentials, "", login_id, "codex") == ["codex", "logout"]
