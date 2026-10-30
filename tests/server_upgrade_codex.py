"""Codex home fixtures written and read through the installed release's login paths."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path


def seed_codex_home(data: Path, member_id: str) -> dict | None:
    from rcp.agents.provider_environment import ProviderCredentialStore
    from rcp.storage import AppStore

    store = AppStore(data / "rcp.sqlite3")
    # The historical interpreter owns the row shape. Earlier releases have no added logins.
    if not hasattr(store, "add_provider_login"):
        return None
    credentials = ProviderCredentialStore.for_data_dir(data)
    logins = [store.add_provider_login("codex", "", added_by=member_id) for _ in range(2)]
    home = credentials.account_dir("codex", "", logins[0].login_id) / "codex-home"
    home.mkdir(parents=True, mode=0o700)
    session_id = str(uuid.uuid4())
    relative = f"sessions/2026/10/01/rollout-2026-10-01T00-00-00-{session_id}.jsonl"
    rollout = home / relative
    rollout.parent.mkdir(parents=True)
    content = (
        json.dumps({"type": "session_meta", "payload": {"id": session_id, "cwd": str(data)}}) + "\n"
    )
    rollout.write_text(content)
    index = json.dumps({"id": session_id, "thread_name": "Retained session"}) + "\n"
    (home / "session_index.jsonl").write_text(index)
    state = home / "state_5.sqlite"
    with sqlite3.connect(state) as database:
        database.execute("CREATE TABLE upgrade_evidence (session_id TEXT)")
        database.execute("INSERT INTO upgrade_evidence VALUES (?)", (session_id,))
    return {
        "session_id": session_id,
        "login_ids": [login.login_id for login in logins],
        "home": str(home),
        "rollout": relative,
        "content": content,
        "index": index,
        "state_sha256": hashlib.sha256(state.read_bytes()).hexdigest(),
    }


def check_codex_home(data: Path, fixture: dict | None, *, migrated: bool) -> None:
    """Run with candidate, then historical Python: both must find the same rollout."""
    if fixture is None:
        return
    from rcp.agents.provider_environment import ProviderCredentialStore
    from rcp.providers.codex.auth import CodexAuthentication

    credentials = ProviderCredentialStore.for_data_dir(data)
    homes = []
    for login_id in fixture["login_ids"]:
        environment = CodexAuthentication().process_environment(credentials, "", login_id)
        assert environment.local_env is not None
        homes.append(Path(environment.local_env["CODEX_HOME"]))
    for home in homes:
        assert (home / fixture["rollout"]).read_text() == fixture["content"]
        assert fixture["index"].strip() in (home / "session_index.jsonl").read_text().splitlines()
    assert (homes[0] / fixture["rollout"]).samefile(homes[1] / fixture["rollout"])
    if migrated:
        retained = homes[0].with_name(homes[0].name + "-retained")
        copies = list(retained.rglob("state_5.sqlite"))
        assert any(
            hashlib.sha256(path.read_bytes()).hexdigest() == fixture["state_sha256"]
            for path in copies
        )
        with sqlite3.connect(copies[0]) as database:
            assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert database.execute("SELECT session_id FROM upgrade_evidence").fetchall() == [
                (fixture["session_id"],)
            ]
