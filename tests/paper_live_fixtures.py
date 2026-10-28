"""Shared disposable live Paper runtime."""

import pytest

from rcp.paper import PaperLive
from rcp.storage import AppStore
from rcp.transport import LocalStateWorkspace


@pytest.fixture(name="paper")
def paper_fixture(tmp_path):
    store = AppStore(tmp_path / "app.sqlite3")
    root = tmp_path / "state"
    root.mkdir()
    workspace = LocalStateWorkspace(root, str(root))
    live = PaperLive(store, lambda _: workspace)
    yield live, store, workspace
    store.close()
