"""The composed startup releases old authenticated work without another sign-in event."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rcp.api import app as app_module

from .helpers import wait_for_task, wait_until
from .provider_login_switch_helpers import switch_background, switched_session
from .test_provider_login_resolver import accounts as accounts


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize(
    ("direction", "machine_row"),
    [("personal_to_team", True), ("team_to_personal", True), ("personal_to_team", False)],
)
def test_startup_releases_old_auth_failure_on_available_login(
    accounts, manifest, tmp_path, monkeypatch, provider, direction, machine_row
):
    stage = tmp_path / "stage"
    _, new, _, _ = switched_session(accounts, stage, provider, direction)
    store = accounts.store
    with store.connection() as connection:
        connection.execute("UPDATE graph_runs SET failure_kind='provider_auth'")
        if not machine_row:
            # A machine sign-in that was never written is implicitly signed in.
            connection.execute("DELETE FROM provider_login_states WHERE login_id=''")
        connection.execute("UPDATE projects SET locator=?", (str(manifest.path),))
    selected = []
    background = switch_background(store, stage, selected)
    monkeypatch.setattr(app_module, "AppStore", lambda _: store)
    monkeypatch.setattr(app_module, "BackgroundAgentTasks", lambda *_, **__: background)
    app = app_module.create_app(data_dir=tmp_path)
    with TestClient(app):
        wait_until(lambda: bool(selected))
        assert selected == [(new, "session", None)]
        # Startup may adopt the manifest's project id, so find the child by parent.
        child = wait_until(lambda: _child_of(store, "previous"))
        wait_for_task(store, child.operation_id, expect="succeeded")


def _child_of(store, parent):
    with store.connection() as connection:
        row = connection.execute(
            "SELECT operation_id FROM graph_runs WHERE parent_operation_id=?", (parent,)
        ).fetchone()
    return store.agent_task(row["operation_id"]) if row else None
