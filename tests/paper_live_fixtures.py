"""Shared disposable live Paper runtime over a real Paper folder."""

import pytest

from rcp.paper import PaperLive
from rcp.paper.folder import LocalPaperFolder
from rcp.paper.folder_io import paper_root
from rcp.storage import AppStore


@pytest.fixture(name="paper")
def paper_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr("rcp.limits.PAPER_POLL_SECONDS", 0.01)
    store = AppStore(tmp_path / "app.sqlite3")
    live = PaperLive(store, lambda project: LocalPaperFolder(paper_root(tmp_path, project)))
    yield live, store, tmp_path / "paper"
    store.close()
