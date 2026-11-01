from __future__ import annotations

from tests.helpers import signed_in_client

from .helpers import create_named_app


def test_legacy_lesson_routes_are_removed(manifest, tmp_path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = signed_in_client(app)
    project_id = app.state.default_project_id
    url = f"/api/projects/{project_id}/lessons"
    assert not any("/lessons" in path for path in app.openapi()["paths"])
    for method, path in (
        ("GET", url),
        ("POST", url),
        ("PATCH", f"{url}/old"),
        ("DELETE", f"{url}/old"),
    ):
        assert client.request(method, path, json={"text": "Old note"}).status_code in {404, 405}
