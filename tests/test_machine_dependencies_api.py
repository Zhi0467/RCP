from __future__ import annotations

import pytest

from rcp.core.models import DependencyStatus, MissingProgram
from tests.helpers import create_named_app, signed_in_client


class _FakeChecker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def status(self, host: str, *, refresh: bool = False) -> DependencyStatus:
        self.calls.append((host, refresh))
        if host:
            return DependencyStatus(
                outcome="missing",
                platform="linux",
                distribution="rocky",
                tested=False,
                missing=(MissingProgram(name="rsync", purpose="sync", required=True),),
                install_notes=("install rsync",),
                checked_at="2026-10-05T00:00:00Z",
            )
        return DependencyStatus(
            outcome="ready", platform="darwin", checked_at="2026-10-05T00:00:00Z"
        )


@pytest.fixture
def app(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    app.state.dependency_checker = _FakeChecker()
    return app


def test_the_card_reads_the_checker_for_local_and_remote_machines(app) -> None:
    client = signed_in_client(app)
    machines = client.get("/api/space/machines").json()["machines"]
    laptop = next(machine for machine in machines if machine["name"] == "laptop")
    remote = client.post(
        "/api/space/machines",
        json={"name": "GPU", "host": "alice@gpu.example", "os_account": "alice"},
    ).json()

    local = client.get(f"/api/space/machines/{laptop['machine_id']}/dependencies")
    assert local.status_code == 200, local.text
    assert local.json()["outcome"] == "ready"
    gpu = client.get(f"/api/space/machines/{remote['machine_id']}/dependencies?refresh=1")
    assert gpu.status_code == 200, gpu.text
    body = gpu.json()
    assert (body["outcome"], body["tested"]) == ("missing", False)
    assert [program["name"] for program in body["missing"]] == ["rsync"]
    assert app.state.dependency_checker.calls == [("", False), ("alice@gpu.example", True)]

    assert client.get("/api/space/machines/absent/dependencies").status_code == 404
    assert len(app.state.dependency_checker.calls) == 2
