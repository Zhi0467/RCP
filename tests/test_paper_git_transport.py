from __future__ import annotations

from pathlib import Path

import pytest

from rcp.paper.git_sync import GitEntry, _git, build_commit, generate_key, read_tree, remote_url


def test_bare_tree_roundtrip_without_checkout(tmp_path: Path):
    project = tmp_path / "transport"
    generate_key(project, "project")
    _git(project, ["init", "--bare", "--template=", str(project / "repo.git")])
    tree = {"paper.tex": GitEntry(b"line\r\n"), "image.png": GitEntry(b"\x00binary")}
    commit = build_commit(project, tree, "", "message", "Member", "member")
    assert read_tree(project, commit) == tree
    assert not (project / "repo.git/paper.tex").exists()
    assert (project / "id_ed25519").stat().st_mode & 0o777 == 0o600
    assert (
        _git(project, ["show", "-s", "--format=%ae", commit]).strip()
        == b"member@members.rcp.invalid"
    )


@pytest.mark.parametrize("repository", ["-option", "../repo", "https://evil/a/b", "a/b/c", "a/b\n"])
def test_remote_rejects_non_github_repository(repository: str):
    with pytest.raises(ValueError):
        remote_url(repository)


def test_remote_url():
    assert remote_url("owner/paper") == "git@github.com:owner/paper.git"


@pytest.mark.parametrize("folder", ["personal", "server"])
def test_transport_hidden_and_write_protected(tmp_path: Path, folder: str):
    from rcp.agents.hidden_read import hidden_read_defaults
    from rcp.agents.write_scope import rcp_owned_paths

    data = tmp_path / folder
    directories, _, _ = hidden_read_defaults(
        home=str(tmp_path),
        app_data_dir=str(data),
        credential_roots=(),
        provider_login_files=(),
        control_socket_dir=None,
    )
    assert str(data / "paper-git") in directories
    assert str(data) in rcp_owned_paths(account_home=str(tmp_path), app_data_dir=data, remote=False)


def test_runner_ignores_ambient_git_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "ambient")
    project = tmp_path / "transport"
    _git(project, ["init", "--bare", "--template=", str(project / "repo.git")])
    assert b"ambient" not in _git(project, ["config", "--list"])


def test_oversized_blob_is_carried_without_reading_its_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from rcp.paper import git_sync

    project = tmp_path / "transport"
    _git(project, ["init", "--bare", "--template=", str(project / "repo.git")])
    commit = build_commit(
        project, {"large": GitEntry(b"large" * 100)}, "", "first", "Member", "member"
    )
    monkeypatch.setattr("rcp.limits.PAPER_MAX_UPLOAD_BYTES", 4)
    original = git_sync._git

    def bounded(project, argv, **kwargs):
        assert argv != ["cat-file", "--batch"]
        return original(project, argv, **kwargs)

    monkeypatch.setattr(git_sync, "_git", bounded)
    tree = read_tree(project, commit)
    assert tree["large"].size == 500
    second = build_commit(project, tree, commit, "carry", "Member", "member")
    assert _git(project, ["rev-parse", commit + "^{tree}"]) == _git(
        project, ["rev-parse", second + "^{tree}"]
    )


def test_host_trust_resource_is_packaged():
    from rcp.paper import git_sync
    from tests.helpers import assert_frozen_backend_ships

    keys = Path(git_sync.__file__).with_name("github_known_hosts").read_text().splitlines()
    assert {line.split()[1] for line in keys} == {"ssh-ed25519", "ecdsa-sha2-nistp256", "ssh-rsa"}
    assert all(line.split()[0] == "github.com" for line in keys)
    assert_frozen_backend_ships("paper/github_known_hosts")
