from __future__ import annotations

import pytest

from rcp.web_assets import WebBuildError, prepared_web_assets


def test_prepared_web_assets_builds_once_without_watch(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr("rcp.web_assets._run_build", lambda: calls.append("build"))

    with prepared_web_assets(watch=False, mode="source"):
        calls.append("serve")

    assert calls == ["build", "serve"]


def test_prepared_web_assets_stops_watcher_after_server_exits(monkeypatch) -> None:
    watcher = object()
    calls = []
    monkeypatch.setattr("rcp.web_assets._start_build_watcher", lambda: watcher)
    monkeypatch.setattr(
        "rcp.web_assets._wait_for_initial_build",
        lambda process, _stamp: calls.append(("ready", process)),
    )
    monkeypatch.setattr(
        "rcp.web_assets._stop_process_group", lambda process: calls.append(("stop", process))
    )

    with prepared_web_assets(watch=True, mode="source"):
        calls.append(("serve", watcher))

    assert calls == [("ready", watcher), ("serve", watcher), ("stop", watcher)]


def test_prebuilt_assets_never_invoke_npm(tmp_path, monkeypatch) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<main>RCP</main>", encoding="utf-8")
    monkeypatch.setattr("rcp.web_assets.web_dist_path", lambda: dist)
    monkeypatch.setattr("rcp.web_assets._run_build", lambda: pytest.fail("npm build was invoked"))
    monkeypatch.setattr(
        "rcp.web_assets._start_build_watcher",
        lambda: pytest.fail("npm watcher was invoked"),
    )

    with prepared_web_assets(watch=False, mode="prebuilt"):
        pass


def test_prebuilt_assets_fail_before_launch_when_bundle_is_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("rcp.web_assets.web_dist_path", lambda: tmp_path / "missing")

    with (
        pytest.raises(WebBuildError, match="prebuilt RCP frontend is missing"),
        prepared_web_assets(watch=False, mode="prebuilt"),
    ):
        pass
