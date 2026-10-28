from __future__ import annotations

from tests.helpers import create_named_app, signed_in_client


def test_project_paper_badge_tracks_the_saved_tree_and_sessions(manifest, tmp_path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    with signed_in_client(app) as client:
        project = f"/api/projects/{app.state.default_project_id}"
        assert client.get(project + "/paper/tree").json()["files"] == []
        initial = client.get(project).json()["paper"]
        assert initial is False
        created = client.post(project + "/paper/files", json={"path": "sections/start.md"})
        assert created.status_code == 200, created.text
        badge = client.get(project).json()["paper"]
        assert badge is True
        assert client.get(project + "/paper/sessions").json() == []
        file_id = created.json()["file_ids"][0]
        assert client.delete(project + f"/paper/files/{file_id}").status_code == 200
        assert client.get(project).json()["paper"] is True


def test_live_paper_writes_do_not_need_a_display_generation(manifest, tmp_path, monkeypatch):
    from rcp.transport import StateUnavailable

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    with signed_in_client(app) as client:
        prefix = f"/api/projects/{app.state.default_project_id}/paper"

        def no_generation(*_args, **_kwargs):
            raise StateUnavailable("Remote state is loading; no completed display generation.")

        monkeypatch.setattr(app.state.services.catalog, "open_display", no_generation)
        created = client.post(prefix + "/files", json={"path": "chapter.md"})
        assert created.status_code == 200, created.text
        assert client.get(prefix + "/tree").json()["files"][0]["path"] == "chapter.md"


def test_mutating_routes_open_the_authoritative_owner() -> None:
    """A write never waits on, or starts from, a remote display generation."""
    import ast
    from pathlib import Path

    import rcp.api

    display = []
    for path in Path(rcp.api.__file__).parent.glob("*.py"):
        for route in ast.walk(ast.parse(path.read_text())):
            if not isinstance(route, ast.FunctionDef | ast.AsyncFunctionDef) or not any(
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr in {"post", "put", "patch", "delete"}
                for decorator in route.decorator_list
            ):
                continue
            display += [
                f"{path.name}:{call.lineno}"
                for call in ast.walk(route)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and (
                    call.func.id == "get_project_service"
                    or call.func.id == "get_graph_service"
                    and not any(keyword.arg == "write" for keyword in call.keywords)
                )
            ]
    assert display == []


def test_source_uploads_are_live_text_documents(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    with signed_in_client(app) as client:
        prefix = f"/api/projects/{app.state.default_project_id}/paper"
        for extension in ("md", "tex", "typ", "bib", "txt", "csv"):
            path = f"source.{extension}"
            response = client.post(
                prefix + "/uploads", data={"path": path}, files={"file": (path, "héllo".encode())}
            )
            assert response.status_code == 200, response.text
            file_id = response.json()["file_ids"][0]
            assert client.get(prefix + f"/files/{file_id}/raw").content == "héllo".encode()
        files = client.get(prefix + "/tree").json()["files"]
        assert len(files) == 6
        assert all(file["kind"] == "text" for file in files)
        with app.state.services.store.connection() as conn:
            assert conn.execute("SELECT COUNT(*) FROM paper_updates").fetchone()[0] == 6


def test_folder_actions_cover_every_file_under_the_folder_and_only_those(manifest, tmp_path):
    import io
    import zipfile

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    with signed_in_client(app) as client:
        prefix = f"/api/projects/{app.state.default_project_id}/paper"
        for path in ("draft/intro.md", "draft/deep/notes.md", "drafts.md"):
            assert client.post(prefix + "/files", json={"path": path}).status_code == 200
        client.post(
            prefix + "/uploads",
            data={"path": "draft/figure.png"},
            files={"file": ("figure.png", b"\x89PNG")},
        )
        revision = client.get(prefix + "/tree").json()["revision"]
        renamed = client.post(prefix + "/folders/rename", json={"path": "draft", "to": "final"})
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["revision"] == revision + 1
        paths = {file["path"] for file in client.get(prefix + "/tree").json()["files"]}
        assert paths == {"final/intro.md", "final/deep/notes.md", "final/figure.png", "drafts.md"}

        archive = client.get(prefix + "/folders/archive", params={"path": "final"})
        assert archive.status_code == 200
        with zipfile.ZipFile(io.BytesIO(archive.content)) as opened:
            assert sorted(opened.namelist()) == [
                "final/deep/notes.md",
                "final/figure.png",
                "final/intro.md",
            ]
            assert opened.read("final/figure.png") == b"\x89PNG"

        assert client.post(prefix + "/folders/delete", json={"path": "final"}).status_code == 200
        assert [file["path"] for file in client.get(prefix + "/tree").json()["files"]] == [
            "drafts.md"
        ]
        for missing in (
            client.post(prefix + "/folders/delete", json={"path": "final"}),
            client.get(prefix + "/folders/archive", params={"path": "final"}),
        ):
            assert missing.status_code == 404
