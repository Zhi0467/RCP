from __future__ import annotations

import gzip
import json
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from rcp.__main__ import EXIT_MIGRATION_UNKNOWN, main
from rcp.storage import AppStore

from .server_upgrade_harness import immutable_fixture_directories, verify_fixture_integrity


def _run_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *argv: str,
) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["rcp", *argv])
    try:
        main()
    except SystemExit as exc:
        code = int(exc.code)
    else:
        code = 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _copy_fixture_database(fixture: Path, destination: Path) -> Path:
    verify_fixture_integrity(fixture)
    copied = destination / "fixture"
    shutil.copytree(fixture, copied)
    data_dir = copied / "data"
    compressed = data_dir / "rcp.sqlite3.gz"
    database = data_dir / "rcp.sqlite3"
    database.write_bytes(gzip.decompress(compressed.read_bytes()))
    compressed.unlink()
    return database


def _event_fields(output: str) -> tuple[dict[str, object], dict[str, object]]:
    records = output.splitlines()
    assert len(records) == 1
    event = json.loads(records[0])
    fields = {item["name"]: item["value"] for item in event["step"]["fields"]}
    return event, fields


def test_migrate_check_reports_a_missing_database_as_fresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = tmp_path / "fresh"

    code, output, errors = _run_cli(
        monkeypatch,
        capsys,
        "migrate",
        "--check",
        "--data-dir",
        str(data_dir),
    )

    assert code == 0
    assert "fresh" in output
    assert "ledger head 0" in output
    assert "no pending migrations" in output
    assert errors == ""
    assert not (data_dir / "rcp.sqlite3").exists()


def test_migrate_check_reports_current_storage_with_no_pending_migrations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = tmp_path / "current"
    AppStore(data_dir / "rcp.sqlite3")

    code, output, errors = _run_cli(
        monkeypatch,
        capsys,
        "migrate",
        "--check",
        "--data-dir",
        str(data_dir),
    )

    head = AppStore._STORAGE_SCHEMA_MIGRATIONS[-1][0]
    assert code == 0
    assert f"ledger head {head}, registry head {head}" in output
    assert "no pending migrations" in output
    assert errors == ""


@pytest.mark.parametrize("fixture", immutable_fixture_directories(), ids=lambda path: path.name)
def test_migrate_check_accepts_every_frozen_server_fixture_without_writing(
    fixture: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = _copy_fixture_database(fixture, tmp_path)
    before = database.read_bytes()

    code, output, errors = _run_cli(
        monkeypatch,
        capsys,
        "migrate",
        "--check",
        "--data-dir",
        str(database.parent),
    )

    assert code == 0
    assert "pending migrations exist" in output
    assert any(name in output for _, name in AppStore._STORAGE_SCHEMA_MIGRATIONS)
    assert errors == ""
    assert database.read_bytes() == before


@pytest.mark.parametrize("unknown", ["name", "head"])
def test_migrate_check_rejects_unknown_ledger_state(
    unknown: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = tmp_path / unknown
    database = data_dir / "rcp.sqlite3"
    AppStore(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        if unknown == "name":
            connection.execute(
                "UPDATE storage_schema_migrations SET migration_name = 'unknown_v1' "
                "WHERE migration_version = 1"
            )
        else:
            head = AppStore._STORAGE_SCHEMA_MIGRATIONS[-1][0]
            connection.execute(
                """
                INSERT INTO storage_schema_migrations(
                    migration_version, migration_name, completed_at
                ) VALUES (?, 'future_schema_v1', '2026-09-02T00:00:00+00:00')
                """,
                (head + 1,),
            )

    code, output, errors = _run_cli(
        monkeypatch,
        capsys,
        "migrate",
        "--check",
        "--data-dir",
        str(data_dir),
    )

    assert code == EXIT_MIGRATION_UNKNOWN
    assert output == ""
    assert "unknown storage state" in errors


def test_migrate_refuses_unknown_state_before_applying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = tmp_path / "unknown-apply"
    database = data_dir / "rcp.sqlite3"
    AppStore(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "UPDATE storage_schema_migrations SET migration_name = 'unknown_v1' "
            "WHERE migration_version = 1"
        )
    before = database.read_bytes()

    code, output, errors = _run_cli(
        monkeypatch,
        capsys,
        "migrate",
        "--data-dir",
        str(data_dir),
    )

    assert code == EXIT_MIGRATION_UNKNOWN
    assert output == ""
    assert "unknown storage state" in errors
    assert database.read_bytes() == before


@pytest.mark.parametrize("state", ["missing-ledger", "invalid-sqlite", "invalid-schema"])
def test_migrate_check_rejects_other_unknown_storage_states(
    state: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = tmp_path / state
    data_dir.mkdir()
    database = data_dir / "rcp.sqlite3"
    if state == "missing-ledger":
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("CREATE TABLE unrelated(value TEXT)")
    elif state == "invalid-sqlite":
        database.write_bytes(b"not a SQLite database")
    else:
        AppStore(database)
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("DROP INDEX watchers_due")

    code, output, errors = _run_cli(
        monkeypatch,
        capsys,
        "migrate",
        "--check",
        "--data-dir",
        str(data_dir),
    )

    assert code == EXIT_MIGRATION_UNKNOWN
    assert output == ""
    assert "unknown storage state" in errors


def test_migrate_applies_a_pre_ledger_fixture_then_check_reports_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = next(
        path
        for path in immutable_fixture_directories()
        if path.name == "pre-storage-migration-ledger-v12-c3191bf"
    )
    database = _copy_fixture_database(fixture, tmp_path)

    code, output, errors = _run_cli(
        monkeypatch,
        capsys,
        "migrate",
        "--data-dir",
        str(database.parent),
    )

    head = AppStore._STORAGE_SCHEMA_MIGRATIONS[-1][0]
    assert code == 0
    assert output == f"RCP migration complete: ledger head {head}.\n"
    assert errors == ""

    code, output, errors = _run_cli(
        monkeypatch,
        capsys,
        "migrate",
        "--check",
        "--data-dir",
        str(database.parent),
    )
    assert code == 0
    assert "no pending migrations" in output
    assert errors == ""


def test_migrate_machine_readable_result_uses_the_server_event_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, output, errors = _run_cli(
        monkeypatch,
        capsys,
        "migrate",
        "--check",
        "--machine-readable",
        "--data-dir",
        str(tmp_path / "fresh"),
    )

    event, fields = _event_fields(output)
    assert code == 0
    assert errors == ""
    assert event["version"] == 1
    assert event["event"] == "step"
    assert event["command"] == "migrate --check"
    assert event["step"]["state"] == "succeeded"
    assert fields == {
        "outcome": "fresh",
        "ledger_head": 0,
        "registry_head": AppStore._STORAGE_SCHEMA_MIGRATIONS[-1][0],
        "pending_migrations": "none",
    }


def test_migrate_refuses_another_process_instance_lock_without_touching_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = tmp_path / "locked"
    database = data_dir / "rcp.sqlite3"
    AppStore(database)
    before = database.read_bytes()
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "from pathlib import Path\n"
                "from rcp.__main__ import instance_lock\n"
                "with instance_lock(Path(sys.argv[1])):\n"
                " print('locked', flush=True)\n"
                " sys.stdin.read(1)\n"
            ),
            str(data_dir),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline() == "locked\n"
    try:
        code, output, errors = _run_cli(
            monkeypatch,
            capsys,
            "migrate",
            "--data-dir",
            str(data_dir),
        )
    finally:
        assert holder.stdin is not None
        holder.stdin.write("x")
        holder.stdin.flush()
        holder.wait(timeout=10)

    assert code != 0
    assert output == ""
    assert "Another RCP process" in errors
    assert database.read_bytes() == before
