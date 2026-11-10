from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from rcp.paper.git_poller import PaperGitPoller


def test_poller_only_pulls_open_linked_projects_and_respects_fence():
    sync = Mock()
    sync.store.paper_git_links.return_value = [
        SimpleNamespace(project_id="open", state="linked"),
        SimpleNamespace(project_id="closed", state="linked"),
        SimpleNamespace(project_id="pending", state="pending"),
    ]
    sync.live.run_sync.return_value = {"open", "pending"}
    fence = SimpleNamespace(active=True)
    poller = PaperGitPoller(sync, startup_effect_fence=fence)
    poller.poll_once()
    sync.pull.assert_not_called()
    fence.active = False
    poller.poll_once()
    sync.pull.assert_called_once_with("open")


def test_poller_failure_backs_off_and_maintenance_blocks():
    sync = Mock()
    sync.store.paper_git_links.return_value = [SimpleNamespace(project_id="open", state="linked")]
    sync.live.run_sync.return_value = {"open"}
    sync.pull.side_effect = ValueError("offline")
    poller = PaperGitPoller(sync)
    poller.poll_once()
    poller.poll_once()
    sync.pull.assert_called_once_with("open")
    poller.admission = SimpleNamespace(closed=True)
    poller.poll_once()
    assert sync.pull.call_count == 1


def test_live_open_projects_requires_a_subscriber(tmp_path):
    import asyncio

    from rcp.paper import PaperLive, PaperOp
    from rcp.storage import AppStore
    from rcp.transport import LocalStateWorkspace
    from tests.paper_helpers import apply, start_paper

    root = tmp_path / "state"
    root.mkdir()
    live = start_paper(
        PaperLive(
            AppStore(tmp_path / "state.sqlite"), lambda _: LocalStateWorkspace(root, str(root))
        )
    )
    apply(live, [PaperOp(kind="create", file_id="f", path="paper.md", text="text")])
    assert live.run_sync(live.open_projects()) == set()

    async def subscribe():
        await live.connect("p", "f", asyncio.Queue())

    live.run_sync(subscribe())
    assert live.run_sync(live.open_projects()) == {"p"}
