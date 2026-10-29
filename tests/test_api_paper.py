from __future__ import annotations

import json

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


def test_a_folder_action_refuses_a_file_added_after_its_files_were_read(manifest, tmp_path):
    from rcp.paper import PaperOp

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    live = app.state.paper_live
    snapshot = live.snapshot

    async def stale_snapshot(project_id, revision=None):
        # A peer adds a file under the folder between the read and the admitted mutation.
        read = await snapshot(project_id, revision)
        await live.apply(project_id, [PaperOp(kind="create", path="draft/late.md")], "peer", {})
        live.snapshot = snapshot
        return read

    with signed_in_client(app) as client:
        prefix = f"/api/projects/{app.state.default_project_id}/paper"
        assert client.post(prefix + "/files", json={"path": "draft/intro.md"}).status_code == 200
        live.snapshot = stale_snapshot
        refused = client.post(prefix + "/folders/delete", json={"path": "draft"})
        assert refused.status_code == 409, refused.text
        paths = {file["path"] for file in client.get(prefix + "/tree").json()["files"]}
        assert paths == {"draft/intro.md", "draft/late.md"}


def test_text_html_previews_inside_the_artifact_sandbox(manifest, tmp_path):
    import html
    import re
    from urllib.parse import parse_qs, urlsplit

    from rcp.artifact_comments import selection_frame_addon
    from rcp.artifact_theme import artifact_color_mode, artifact_theme
    from rcp.artifact_views import InlineAppearance, artifact_content

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    with signed_in_client(app) as client:
        prefix = f"/api/projects/{app.state.default_project_id}/paper"
        page = b"<!doctype html><p id=mark>hello</p>"
        ids = {}
        for path, data in (("page.html", page), ("blob.htm", b"\x00\xff")):
            uploaded = client.post(prefix + "/uploads", data={"path": path}, files={"file": data})
            ids[path] = uploaded.json()["file_ids"][0]
        formats = {
            file["path"]: file["format"] for file in client.get(prefix + "/tree").json()["files"]
        }
        assert formats == {"page.html": "html", "blob.htm": "other"}
        revision = client.get(prefix + "/tree").json()["revision"]
        file_url = prefix + f"/files/{ids['page.html']}"

        shell = client.get(file_url + "/preview", params={"revision": revision})
        assert shell.status_code == 200
        config = re.search(r"const inlineConfig=(\{.*?\});", shell.text)
        assert config
        assert json.loads(config.group(1))["selectable"] is True
        target = urlsplit(json.loads(config.group(1))["contentUrl"])
        assert target.path == file_url + "/preview/content"
        assert parse_qs(target.query)["revision"] == [str(revision)]

        content = client.get(file_url + "/preview/content", params={"revision": revision})
        _, media_type, csp = artifact_content(
            "page.html", "text/html", page, frame_addon=selection_frame_addon()
        )
        assert content.status_code == 200
        assert content.headers["content-security-policy"] == csp
        assert content.headers["content-type"].startswith(media_type)
        assert content.headers["x-content-type-options"] == "nosniff"
        framed = re.search(
            r'<iframe id="artifact" sandbox="allow-scripts" srcdoc="([^"]*)"', content.text
        )
        assert framed and 'id="mark">hello' in html.unescape(framed.group(1))
        # Inside the Paper pane the page paints with the app's theme, as an inline artifact does.
        inline = client.get(
            file_url + "/preview/content",
            params={"revision": revision, "presentation": "inline", "color_mode": "dark"},
        )
        themed, _, _ = artifact_content(
            "page.html",
            "text/html",
            page,
            frame_addon=selection_frame_addon(),
            inline=InlineAppearance(artifact_theme(None), artifact_color_mode("dark")),
        )
        assert inline.text == themed != content.text

        for missing in (
            client.get(file_url + "/preview/content", params={"revision": revision + 99}),
            client.get(prefix + f"/files/{ids['blob.htm']}/preview"),
            client.get(prefix + "/files/unknown/preview/content"),
        ):
            assert missing.status_code == 404
        raw = client.get(file_url + "/raw")
        assert raw.headers["content-disposition"].startswith("attachment")


def test_html_preview_is_member_only(tmp_path):
    from tests.test_project_membership import _create_project, _team_app

    app, client, store, people, acting = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "repo")
    prefix = f"/api/projects/{project_id}/paper"
    with client:
        page = client.post(prefix + "/uploads", data={"path": "p.html"}, files={"file": b"<p>x"})
        file_url = prefix + f"/files/{page.json()['file_ids'][0]}/preview"
        assert client.get(file_url + "/content").status_code == 200
        acting[0] = people[1].user_id
        assert client.get(file_url).status_code == 404
        assert client.get(file_url + "/content").status_code == 404
