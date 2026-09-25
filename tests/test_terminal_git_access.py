from __future__ import annotations

from pathlib import Path

from rcp.terminals.git_access import terminal_git_access


def test_existing_git_access_is_read_only_input_without_copying_secrets(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".ssh").mkdir()
    (home / ".gitconfig").write_text("[user]\nname = Test\n")
    key = tmp_path / "key with spaces"
    key.write_text("test-secret")
    monkeypatch.setattr(Path, "home", lambda: home)

    paths, environment = terminal_git_access(key)

    assert set(paths) == {str(home / ".ssh"), str(home / ".gitconfig"), str(key)}
    assert environment == {}
    assert "test-secret" not in str((paths, environment))
    assert key.read_text() == "test-secret"


def test_personal_repository_uses_existing_git_config_without_managed_key(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert terminal_git_access(None) == ((), {})
