from __future__ import annotations

import fnmatch
import os
from pathlib import Path

import pytest

from rcp.agents import hidden_read
from rcp.agents.hidden_read import (
    HIDDEN_READ_ENV_ALLOW_LIST,
    HiddenFolderRejected,
    hidden_read_defaults,
    resolve_hidden_read_scope,
    validate_machine_hidden_folders,
)
from rcp.config import Manifest
from rcp.core.models import HiddenReadKeyEvidence
from rcp.limits import HIDDEN_READ_PATH_MAX_COUNT, HIDDEN_READ_PATH_MAX_LENGTH


def test_defaults_hide_owned_secrets_without_operational_paths() -> None:
    directories, files, globs = hidden_read_defaults(
        home="/home/research",
        app_data_dir="/data",
        credential_roots=("/provider-store",),
        provider_login_files=("/provider/auth.json",),
        control_socket_dir="/control",
    )

    def hidden(path):
        return (
            path in files
            or any(path == root or path.startswith(root + "/") for root in directories)
            or any(fnmatch.fnmatchcase(path, pattern) for pattern in globs)
        )

    for path in (
        "/data/rcp.sqlite3-wal",
        "/data/checkpoints/operation/payload/app-data/rcp.sqlite3",
        "/data/run-stage/backup-123/rcp.sqlite3",
        "/data/providers/claude/token",
        "/data/service-connections/member/connections/id/key",
        "/provider/auth.json",
        "/control/socket",
        "/home/research/.config/rcp/claude-setup-token",
    ):
        assert hidden(path)
    for path in (
        "/data/tools/tool",
        "/data/run-stage/task/workspace/file",
        "/home/research/.rcp/sockets/command.sock",
        "/home/research/.ssh/id_ed25519",
        "/home/research/.ssh/id_ed25519.pub",
        "/home/research/.ssh/known_hosts",
        "/home/research/.config/gh/hosts.yml",
        "/home/research/.aws/credentials",
        "/home/research/.netrc",
    ):
        assert not hidden(path)
    assert {
        "SSH_AUTH_SOCK",
        "GIT_CONFIG_SYSTEM",
        "PLAYWRIGHT_CLI_SESSION",
        "PLAYWRIGHT_BROWSERS_PATH",
    } <= set(HIDDEN_READ_ENV_ALLOW_LIST)
    assert not {"CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY", "GH_TOKEN", "BASH_ENV"} & set(
        HIDDEN_READ_ENV_ALLOW_LIST
    )


@pytest.mark.parametrize("folder", ["/protected", "/protected/child", "/"])
def test_overlap_is_refused_in_both_directions(folder: str) -> None:
    with pytest.raises(HiddenFolderRejected) as rejected:
        validate_machine_hidden_folders([folder], protected_roots=("/protected",))
    assert rejected.value.code == "protected_root_overlap"


@pytest.mark.parametrize(
    "folders,code",
    [
        (["relative"], "invalid_hidden_folder"),
        (["/a/../b"], "invalid_hidden_folder"),
        (["/a\x00"], "invalid_hidden_folder"),
        (["/" + "x" * HIDDEN_READ_PATH_MAX_LENGTH], "invalid_hidden_folder"),
        ([f"/a/{n}" for n in range(HIDDEN_READ_PATH_MAX_COUNT + 1)], "too_many_hidden_folders"),
        (["/a", "/a"], "duplicate_hidden_folder"),
    ],
)
def test_folder_validation_is_bounded_and_unique(folders, code) -> None:
    with pytest.raises(HiddenFolderRejected) as rejected:
        validate_machine_hidden_folders(folders, protected_roots=())
    assert rejected.value.code == code


def test_folder_symlinks_cannot_bypass_protected_roots(tmp_path: Path) -> None:
    protected = tmp_path / "protected"
    protected.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(protected, target_is_directory=True)
    with pytest.raises(HiddenFolderRejected):
        validate_machine_hidden_folders([str(alias)], protected_roots=(str(protected),))
    assert validate_machine_hidden_folders([str(alias)], protected_roots=()) == [
        str(protected.resolve())
    ]


def _resolve(manifest, tmp_path, monkeypatch, *, readiness=None, **changes):
    def facts(remote_stage, paths):
        return {
            "home": str(tmp_path / "home"),
            "os_account": "research",
            "provider_login_files": [str(tmp_path / "home/.codex/auth.json")],
            "paths": {path: os.path.realpath(path) for path in paths},
            "readiness": readiness or {"ready": True, "platform": "linux", "reason": None},
        }

    monkeypatch.setattr(hidden_read, "_facts", facts)
    return resolve_hidden_read_scope(
        **{
            "manifest": manifest,
            "execution_machine": "laptop",
            "provider": "claude",
            "capability": "discuss",
            "stage_root": str(tmp_path / "data/run-stage/task"),
            "workspace_root": str(tmp_path / "data/run-stage/task/workspace"),
            "app_data_dir": tmp_path / "data",
            "remote_stage": None,
            "repository_inventory": [],
            "machine_hidden_folders": [],
            "key_evidence": (),
            "browser_enabled": False,
            **changes,
        }
    )


def test_adding_folder_changes_fingerprint(manifest: Manifest, tmp_path: Path, monkeypatch) -> None:
    first = _resolve(manifest, tmp_path, monkeypatch)
    second = _resolve(
        manifest, tmp_path, monkeypatch, machine_hidden_folders=[str(tmp_path / "private")]
    )
    assert first.fingerprint != second.fingerprint
    assert str(tmp_path / "private") in second.hidden_directories
    assert second.enforcement.status == "enforced"


@pytest.mark.parametrize(
    "ready,platform,reason,browser,expected",
    [
        (False, "linux", "wrapper_unavailable", False, "wrapper_unavailable"),
        (False, "linux", "userns_blocked", False, "userns_blocked"),
        (True, "darwin", None, True, "browser_unwrapped_macos"),
    ],
)
def test_readiness_gaps_are_visible(
    manifest, tmp_path, monkeypatch, ready, platform, reason, browser, expected
):
    scope = _resolve(
        manifest,
        tmp_path,
        monkeypatch,
        readiness={"ready": ready, "platform": platform, "reason": reason},
        browser_enabled=browser,
    )
    assert scope.enforcement.status == "unhidden"
    assert expected in scope.enforcement.reasons


@pytest.mark.parametrize("kind", ["deploy_key", "ssh_identity"])
@pytest.mark.parametrize("confirmed", [False, True])
def test_only_confirmed_keys_hidden_and_exempt_parents_stay_readable(
    manifest, tmp_path, monkeypatch, kind, confirmed
):
    path = str(tmp_path / "data/providers/key")
    key = HiddenReadKeyEvidence(
        path=path,
        kind=kind,
        agent_confirmed=confirmed,
        visibility="hidden" if confirmed else "readable",
        public_key_fingerprint="SHA256:" + "A" * 43 if confirmed else None,
    )
    scope = _resolve(manifest, tmp_path, monkeypatch, key_evidence=(key,))
    assert (path in scope.hidden_files) == confirmed
    assert (str(tmp_path / "data/providers") in scope.hidden_directories) == confirmed
    assert (scope.enforcement.status == "enforced") == confirmed
    if not confirmed:
        with pytest.raises(HiddenFolderRejected):
            _resolve(
                manifest,
                tmp_path,
                monkeypatch,
                key_evidence=(key,),
                machine_hidden_folders=[str(Path(path).parent)],
            )


def test_remote_canonicalization_uses_shipped_source(manifest, tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace

    calls = []

    def ssh(argv):
        import json

        calls.append(argv)
        paths = json.loads(argv[-1])
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {
                    "home": "/remote/home",
                    "os_account": "remote",
                    "provider_login_files": [],
                    "readiness": {"ready": True, "platform": "linux", "reason": None},
                    "paths": {path: path.replace("/alias", "/remote/private") for path in paths},
                }
            ),
            "",
        )

    manifest.machines[0].host = "remote.example"
    remote = SimpleNamespace(host="remote.example", _ssh=ssh)
    scope = resolve_hidden_read_scope(
        manifest=manifest,
        execution_machine="laptop",
        provider="claude",
        capability="discuss",
        stage_root="/remote/stage",
        workspace_root="/remote/stage/workspace",
        app_data_dir=tmp_path / "local-data",
        remote_stage=remote,
        repository_inventory=[],
        machine_hidden_folders=["/alias"],
        key_evidence=(),
        browser_enabled=False,
    )
    assert "/remote/private" in scope.hidden_directories
    assert not any(str(tmp_path / "local-data") in path for path in scope.hidden_globs)
    assert scope.os_account == "remote"
    assert all(call[2] == hidden_read.staged_hidden_read_source() for call in calls)


def test_installed_copies_and_native_tool_gap(manifest, tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        hidden_read,
        "installed_server_storage",
        lambda path: SimpleNamespace(
            update_checkpoints_root=str(tmp_path / "checkpoints"),
            restore_operations_root=str(tmp_path / "restores"),
        ),
    )
    scope = _resolve(
        manifest,
        tmp_path,
        monkeypatch,
        provider="opencode",
        machine_hidden_folders=[str(tmp_path / "literal[folder]")],
    )
    assert str(tmp_path / "checkpoints") + "/**/rcp.sqlite3*" in scope.hidden_globs
    assert "provider_native_tools_uncovered" in scope.enforcement.reasons
