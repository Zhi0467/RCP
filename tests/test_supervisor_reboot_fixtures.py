from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import rcp.storage.models as storage_models
from rcp.storage import AppStore
from tests.supervisor_reboot_build import MIGRATION_TABLE, add_forward_migration
from tests.supervisor_reboot_data import prepare_data


@pytest.mark.parametrize("initialized", [False, True])
def test_disposable_data_has_canonical_project_retained_stage_and_attachment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initialized: bool,
) -> None:
    # The actual guest uses the production layout and rcp account; this fixture
    # test substitutes only that host layout, not the project or backup owners.
    account = pwd.getpwuid(os.geteuid()).pw_name
    monkeypatch.setattr(
        storage_models,
        "DEFAULT_SERVER_LAYOUT",
        SimpleNamespace(service_account=account, projects_root=tmp_path / "projects"),
    )
    code = None
    original_space = None
    if initialized:
        original, code = AppStore.initialize_team_space(
            tmp_path / "data" / "rcp.sqlite3", "Reboot qualification"
        )
        original_space = original.space_id
    receipt = prepare_data(
        tmp_path / "data", tmp_path / "projects", account=account, bootstrap_code=code
    )
    if initialized:
        assert receipt["space_id"] == original_space
    store = AppStore.open_read_only(tmp_path / "data" / "rcp.sqlite3")
    assert store.authenticate_team_member_token(receipt["token"]).user_id == receipt["member_id"]
    assert (Path(receipt["stage"]) / "retained.txt").is_file()
    assert (Path(receipt["research"]) / "manifest.toml").is_file()
    assert receipt["attachment_id"]


def test_qualification_target_really_migrates_and_old_code_refuses_it(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[1]
    copied = tmp_path / "candidate"
    shutil.copytree(
        workspace / "src" / "rcp",
        copied / "rcp",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    ledger = add_forward_migration(copied / "rcp" / "storage" / "base.py")
    database = tmp_path / "data" / "rcp.sqlite3"
    script = (
        "import json, sys; from pathlib import Path; from rcp.storage import AppStore; "
        "s=AppStore(Path(sys.argv[1])); "
        "print(json.dumps({'ledger':s.storage_schema_ledger_head()}))"
    )

    def open_store(source: Path):
        environment = {**os.environ, "PYTHONPATH": str(source)}
        return subprocess.run(
            [sys.executable, "-c", script, str(database)],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            cwd=tmp_path,
            timeout=60,
        )

    base = open_store(workspace / "src")
    assert base.returncode == 0, base.stderr
    assert json.loads(base.stdout)["ledger"] == ledger - 1
    candidate = open_store(copied)
    assert candidate.returncode == 0, candidate.stderr
    assert json.loads(candidate.stdout)["ledger"] == ledger
    import sqlite3

    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name=?", (MIGRATION_TABLE,)
        ).fetchone() == (MIGRATION_TABLE,)
    old = open_store(workspace / "src")
    assert old.returncode != 0
    assert "migration ledger is invalid" in old.stderr
