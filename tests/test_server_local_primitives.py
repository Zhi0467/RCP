from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from rcp.server_ops import _local_primitives as primitives


def test_canonical_json_uuid_and_path_primitives() -> None:
    assert primitives.canonical_json_bytes({"z": 1, "a": "é"}) == b'{"a":"\xc3\xa9","z":1}'
    assert primitives.canonical_json_line({"b": 2, "a": 1}) == b'{"a":1,"b":2}\n'

    identifier = str(uuid.uuid4())
    assert primitives.canonical_uuid4(identifier, label="identity") == identifier
    assert primitives.is_canonical_uuid4(identifier)
    assert not primitives.is_canonical_uuid4(identifier.upper())
    assert not primitives.is_canonical_uuid4("not-a-uuid")

    assert primitives.normalized_absolute_path("/", label="path") == "/"
    assert primitives.normalized_absolute_non_root_path("/srv/rcp", label="path") == "/srv/rcp"
    for invalid in ("/", "/srv/../tmp", "/srv//rcp", "relative"):
        with pytest.raises(ValueError):
            primitives.normalized_absolute_non_root_path(invalid, label="path")


def test_write_all_retries_short_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[bytes] = []

    def short_write(_descriptor: int, payload: memoryview) -> int:
        chunk = bytes(payload[:2])
        observed.append(chunk)
        return len(chunk)

    monkeypatch.setattr(primitives.os, "write", short_write)
    primitives.write_all(7, b"abcde")
    assert b"".join(observed) == b"abcde"


def test_tree_sync_contracts_keep_directory_only_and_full_sync_distinct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = tmp_path / "child"
    child.mkdir()
    payload = child / "payload.bin"
    payload.write_bytes(b"payload")
    synced_files: list[Path] = []
    synced_directories: list[Path] = []
    monkeypatch.setattr(primitives, "fsync_file", synced_files.append)
    monkeypatch.setattr(primitives, "fsync_directory", synced_directories.append)

    primitives.fsync_directory_tree(tmp_path)
    assert synced_files == []
    assert synced_directories == [child, tmp_path]

    synced_directories.clear()
    primitives.fsync_file_tree(tmp_path)
    assert synced_files == [payload]
    assert synced_directories == [child, tmp_path]


def test_private_file_reader_checks_metadata_and_bytes(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    path.write_bytes(b'{"ok":true}\n')
    path.chmod(0o600)

    assert (
        primitives.read_stable_private_file(
            path,
            expected_uid=os.geteuid(),
            expected_mode=0o600,
            maximum=1024,
            chunk_size=3,
        )
        == b'{"ok":true}\n'
    )

    path.chmod(0o644)
    with pytest.raises(primitives.PrivateFileReadError, match="unsafe"):
        primitives.read_stable_private_file(
            path,
            expected_uid=os.geteuid(),
            expected_mode=0o600,
            maximum=1024,
            chunk_size=3,
        )
