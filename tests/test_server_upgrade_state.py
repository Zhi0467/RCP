from __future__ import annotations

import io
import json
from contextlib import contextmanager

from tests.server_upgrade_state import seed_edited_branch


def test_edited_branch_is_skipped_on_a_release_that_only_lists_branches() -> None:
    # 0.4.17 to 0.4.24 serve GET graph-refs; creating a branch arrived in 0.4.25.
    calls: list[tuple[str, str]] = []

    @contextmanager
    def request(path: str, method: str = "GET", **_kwargs):
        calls.append((method, path))
        paths = {"/api/projects/{project_id}/graph-refs": {"get": {}}}
        yield io.BytesIO(json.dumps({"paths": paths}).encode())

    fixture = {"project_id": "p1", "upgrade_state": {"schedule": True}}
    assert seed_edited_branch(request, fixture, cookie="c") is None
    assert calls == [("GET", "/openapi.json")]
