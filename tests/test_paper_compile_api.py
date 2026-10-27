from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from rcp.api import paper_compile as api
from rcp.api.dependencies import require_project_membership, require_project_write_admission
from rcp.paper.compile import CompileResult


@pytest.fixture
def route_client(tmp_path, monkeypatch):
    state = SimpleNamespace(admitted=True, member=True, calls=[], installs=[])

    def membership(project_id: str):
        if not state.member:
            raise HTTPException(404)
        return "canonical" if project_id == "alias" else project_id

    def admission(project_id: str):
        if not state.admitted:
            raise HTTPException(409)
        yield membership(project_id)

    async def compile(project, file, revision):
        state.calls.append((project, file, revision))
        return CompileResult(compile_id="result", status="ok", pdf_url="pdf")

    def pdf(project, compile_id):
        if (project, compile_id) != ("canonical", "result"):
            raise KeyError(compile_id)
        return b"%PDF-1.7\n"

    asset_path = tmp_path / "compiler.wasm"
    asset_path.write_bytes(b"wasm")

    def asset(name):
        if name != "compiler.wasm":
            raise ValueError(name)
        return asset_path

    app = FastAPI()
    app.include_router(api.router)
    app.include_router(api.assets_router)
    app.dependency_overrides[require_project_membership] = membership
    app.dependency_overrides[require_project_write_admission] = admission
    app.dependency_overrides[api.get_paper_compiler] = lambda: SimpleNamespace(
        compile=compile, pdf=pdf
    )
    app.dependency_overrides[api.get_paper_engines] = lambda: SimpleNamespace(
        status=lambda name: "ready",
        error=lambda name: "",
        asset=asset,
        start_install=state.installs.append,
    )
    monkeypatch.setattr(
        api,
        "get_identity_access",
        lambda request: SimpleNamespace(acting_user=lambda request: membership("assets")),
    )
    with TestClient(app) as client:
        yield client, state


def test_compile_uses_canonical_project_and_write_admission(route_client):
    client, state = route_client
    url = "/api/projects/alias/paper/compile"
    body = {"engine": "latex", "file_id": "file", "revision": 4}
    assert client.post(url, json=body).status_code == 200
    assert state.calls == [("canonical", "file", 4)]
    state.admitted = False
    assert client.post(url, json=body).status_code == 409
    assert len(state.calls) == 1
    state.member = False
    assert client.post(url, json=body).status_code == 404


def test_pdf_is_project_scoped_and_nosniff(route_client):
    client, state = route_client
    url = "/api/projects/alias/paper/compile/result/pdf"
    response = client.get(url)
    assert response.content.startswith(b"%PDF")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert client.get(url.replace("alias", "other")).status_code == 404
    state.member = False
    assert client.get(url).status_code == 404


def test_engine_install_admission_and_asset_serving(route_client):
    client, state = route_client
    assert client.get("/api/projects/alias/paper/engines").json()["typst"] == "ready"
    url = "/api/projects/alias/paper/engines/typst/install"
    assert client.post(url).status_code == 202
    assert state.installs == ["typst"]
    state.admitted = False
    assert client.post(url).status_code == 409
    response = client.get("/api/paper-assets/typst/compiler.wasm")
    assert response.content == b"wasm"
    assert response.headers["content-type"] == "application/wasm"
    assert "immutable" in response.headers["cache-control"]
    assert client.get("/api/paper-assets/typst/missing").status_code == 404
    state.member = False
    assert client.get("/api/paper-assets/typst/compiler.wasm").status_code == 404
