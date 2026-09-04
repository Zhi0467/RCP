from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from rcp.server_ops import backup_identity as identity_owner
from rcp.server_ops.backup_identity import (
    BackupIdentityRefused,
    backup_identity_path,
    resolve_backup_recipient,
)

AGE_RECIPIENT = "age1qypqxpq9qcrsszg2pvxq6rs0zqg3yyc5z5tpwxqergd3c8g7rusqmwn7f2"
OTHER_RECIPIENT = "age1qgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpquuzgag"


def _layout(tmp_path: Path):
    config_dir = tmp_path / "etc" / "rcp"
    config_dir.mkdir(parents=True, mode=0o755)
    return type("Layout", (), {"config_path": config_dir / "server.toml"})()


def _fake_age_keygen(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    def run(argv, **_kwargs):
        command = tuple(str(value) for value in argv)
        calls.append(command)
        if "--output" in command:
            output = Path(command[-1])
            output.write_text("AGE-SECRET-KEY-1TEST\n", encoding="ascii")
            os.chmod(output, 0o600)
            return subprocess.CompletedProcess(command, 0)
        if command[1:2] == ("-y",):
            identity = Path(command[-1])
            if identity.read_text(encoding="ascii") != "AGE-SECRET-KEY-1TEST\n":
                return subprocess.CompletedProcess(command, 1, "", "")
            return subprocess.CompletedProcess(command, 0, f"{AGE_RECIPIENT}\n", "")
        raise AssertionError(command)

    monkeypatch.setattr(identity_owner.subprocess, "run", run)
    return calls


def test_first_simple_configuration_creates_and_reuses_one_root_owned_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = _fake_age_keygen(monkeypatch)
    layout = _layout(tmp_path)
    owner = (os.getuid(), os.getgid())

    first = resolve_backup_recipient(
        layout=layout,
        configured_recipient=None,
        requested_recipient=None,
        expected_owner=owner,
    )
    path = backup_identity_path(layout)
    first_inode = path.stat().st_ino
    second = resolve_backup_recipient(
        layout=layout,
        configured_recipient=AGE_RECIPIENT,
        requested_recipient=None,
        expected_owner=owner,
    )

    assert first == second == AGE_RECIPIENT
    assert path.stat().st_ino == first_inode
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sum("--output" in call for call in calls) == 1


def test_damaged_identity_fails_without_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _fake_age_keygen(monkeypatch)
    layout = _layout(tmp_path)
    path = backup_identity_path(layout)
    path.write_text("damaged\n", encoding="ascii")
    os.chmod(path, 0o600)
    before = path.read_bytes()

    with pytest.raises(BackupIdentityRefused, match="damaged"):
        resolve_backup_recipient(
            layout=layout,
            configured_recipient=AGE_RECIPIENT,
            requested_recipient=None,
            expected_owner=(os.getuid(), os.getgid()),
        )

    assert path.read_bytes() == before


def test_failed_generation_leaves_no_identity_or_partial_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)

    def fail_after_write(argv, **_kwargs):
        output = Path(argv[-1])
        output.write_text("AGE-SECRET-KEY-1PARTIAL\n", encoding="ascii")
        return subprocess.CompletedProcess(argv, 1)

    monkeypatch.setattr(identity_owner.subprocess, "run", fail_after_write)

    with pytest.raises(BackupIdentityRefused, match="could not create"):
        resolve_backup_recipient(
            layout=layout,
            configured_recipient=None,
            requested_recipient=None,
            expected_owner=(os.getuid(), os.getgid()),
        )

    assert list(layout.config_path.parent.iterdir()) == []


def test_retained_identity_refuses_a_different_recipient(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _fake_age_keygen(monkeypatch)
    layout = _layout(tmp_path)
    owner = (os.getuid(), os.getgid())
    assert (
        resolve_backup_recipient(
            layout=layout,
            configured_recipient=None,
            requested_recipient=None,
            expected_owner=owner,
        )
        == AGE_RECIPIENT
    )

    with pytest.raises(BackupIdentityRefused, match="differs"):
        resolve_backup_recipient(
            layout=layout,
            configured_recipient=AGE_RECIPIENT,
            requested_recipient=OTHER_RECIPIENT,
            expected_owner=owner,
        )


def test_existing_external_configuration_requires_its_explicit_recipient(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)

    with pytest.raises(BackupIdentityRefused, match="externally managed"):
        resolve_backup_recipient(
            layout=layout,
            configured_recipient=AGE_RECIPIENT,
            requested_recipient=None,
            expected_owner=(os.getuid(), os.getgid()),
        )

    assert (
        resolve_backup_recipient(
            layout=layout,
            configured_recipient=AGE_RECIPIENT,
            requested_recipient=AGE_RECIPIENT,
            expected_owner=(os.getuid(), os.getgid()),
        )
        == AGE_RECIPIENT
    )
    assert not backup_identity_path(layout).exists()

    with pytest.raises(BackupIdentityRefused, match="rotation is not implicit"):
        resolve_backup_recipient(
            layout=layout,
            configured_recipient=AGE_RECIPIENT,
            requested_recipient=OTHER_RECIPIENT,
            expected_owner=(os.getuid(), os.getgid()),
        )


def test_unsafe_identity_permissions_fail_loudly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _fake_age_keygen(monkeypatch)
    layout = _layout(tmp_path)
    path = backup_identity_path(layout)
    path.write_text("AGE-SECRET-KEY-1TEST\n", encoding="ascii")
    os.chmod(path, 0o644)

    with pytest.raises(BackupIdentityRefused, match="mode-0600"):
        resolve_backup_recipient(
            layout=layout,
            configured_recipient=AGE_RECIPIENT,
            requested_recipient=None,
            expected_owner=(os.getuid(), os.getgid()),
        )
