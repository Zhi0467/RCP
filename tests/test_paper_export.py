from __future__ import annotations

import asyncio
import io
import threading
import zipfile
from pathlib import Path

import pytest

from rcp.paper import PaperOp
from rcp.paper.compile import PaperCompiler
from rcp.paper.engines import EngineStore
from tests.helpers import async_wait_until, create_named_app, signed_in_client
from tests.test_paper_compile import Live


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["bbl", "empty", "failed", "timeout", "unavailable"])
async def test_bibliography_result(tmp_path, monkeypatch, mode):
    async def ensure(name):
        return mode != "unavailable"

    engines = EngineStore(tmp_path)
    monkeypatch.setattr(engines, "ensure", ensure)
    compiler = PaperCompiler(Live(), engines, tmp_path)

    async def run(tree, output, root, *, keep_intermediates=False):
        assert keep_intermediates
        assert (tree / root).read_text().startswith(r"\documentclass")
        if mode == "timeout":
            raise TimeoutError
        if mode == "bbl":
            (output / "paper.bbl").write_bytes(b"bibliography")
        return (1 if mode == "failed" else 0), "diagnostic"

    monkeypatch.setattr(compiler, "_run", run)
    result = await compiler.bibliography("project", "paper.tex")
    expected = (
        "unavailable"
        if mode == "unavailable"
        else "failed"
        if mode in {"failed", "timeout"}
        else "ok"
    )
    assert result.status == expected
    assert result.bbl == (b"bibliography" if mode == "bbl" else None)
    if mode in {"failed", "timeout"}:
        assert result.log_tail


@pytest.mark.asyncio
async def test_export_waits_for_compile_slot_without_cancelling_preview(tmp_path, monkeypatch):
    async def ensure(name):
        return True

    engines = EngineStore(tmp_path)
    monkeypatch.setattr(engines, "ensure", ensure)
    compiler = PaperCompiler(Live(), engines, tmp_path)
    compiler._slots = asyncio.Semaphore(1)
    preview_started = asyncio.Event()
    release = asyncio.Event()
    exports = []

    async def run(tree, output, root, *, keep_intermediates=False):
        if keep_intermediates:
            exports.append(root)
            (output / "paper.bbl").write_bytes(b"references")
        else:
            preview_started.set()
            await release.wait()
            (output / "paper.pdf").write_bytes(b"%PDF-1.7 preview")
        return 0, ""

    monkeypatch.setattr(compiler, "_run", run)
    preview = asyncio.create_task(compiler.compile("project", "file"))
    await preview_started.wait()
    export = asyncio.create_task(compiler.bibliography("project", "paper.tex"))
    await asyncio.sleep(0)
    assert not exports
    assert not preview.done()
    release.set()
    assert (await preview).status == "ok"
    assert (await export).bbl == b"references"
    await compiler.close()


@pytest.mark.parametrize(
    "mode", ["bbl", "empty", "existing", "concurrent", "failed", "unavailable"]
)
def test_arxiv_export_route(manifest, tmp_path, monkeypatch, mode):
    async def ensure(self, name):
        assert name == "latex"
        if mode == "concurrent":
            await app.state.paper_live.apply(
                app.state.default_project_id,
                [PaperOp(kind="rename", file_id=ids["paper/main.tex"], path="renamed.tex")],
                "human",
            )
        return mode != "unavailable"

    async def run(self, tree, output, root, *, keep_intermediates=False):
        assert keep_intermediates
        assert root == "paper/main.tex"
        assert (tree / "paper/refs.bib").read_bytes() == b"@article{key}"
        if mode != "empty":
            (output / "main.bbl").write_bytes(b"generated references")
        return (1 if mode == "failed" else 0), "diagnostic"

    monkeypatch.setattr(EngineStore, "ensure", ensure)
    monkeypatch.setattr(PaperCompiler, "_run", run)
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    with signed_in_client(app) as client:
        prefix = f"/api/projects/{app.state.default_project_id}/paper"
        files = {
            "paper/main.tex": rb"\documentclass{article}\input{section}",
            "paper/section.tex": b"section",
            "paper/refs.bib": b"@article{key}",
            "paper/style.sty": b"style",
            "paper/article.cls": b"class",
            "paper/plain.bst": b"bst",
            "figures/image.png": b"\x89PNG\x00",
            "figures/plot.pdf": b"%PDF-1.7",
            "notes.md": b"notes",
            "other.typ": b"typst",
            "page.html": b"<p>html</p>",
            "paper/main.aux": b"auxiliary",
        }
        if mode == "existing":
            files["paper/main.bbl"] = b"saved references"
        ids = {}
        for path, content in files.items():
            response = client.post(
                prefix + "/uploads", data={"path": path}, files={"file": (Path(path).name, content)}
            )
            assert response.status_code == 200, response.text
            ids[path] = response.json()["file_ids"][0]
        for path in ("notes.md", "paper/section.tex"):
            response = client.get(prefix + "/export/arxiv", params={"file_id": ids[path]})
            assert response.status_code == 409
            assert response.json() == {"reason": "not_root", "log_tail": ""}
        assert (
            client.get(prefix + "/export/arxiv", params={"file_id": "missing"}).status_code == 404
        )
        response = client.get(prefix + "/export/arxiv", params={"file_id": ids["paper/main.tex"]})
        if mode in {"failed", "unavailable"}:
            assert response.status_code == 409
            assert response.json()["reason"] == mode
            assert "log_tail" in response.json()
            return
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "application/zip"
        assert response.headers["x-content-type-options"] == "nosniff"
        expected = {
            path: data
            for path, data in files.items()
            if path not in {"notes.md", "other.typ", "page.html", "paper/main.aux"}
        }
        if mode in {"bbl", "concurrent"}:
            expected["paper/main.bbl"] = b"generated references"
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert {name: archive.read(name) for name in archive.namelist()} == expected


def test_real_tectonic_export_compiles_offline(manifest, tmp_path, monkeypatch):
    import os
    import subprocess

    executable = os.environ.get("RCP_TEST_PAPER_EXPORT_TECTONIC")
    if not executable:
        pytest.skip("No Tectonic binary supplied for export verification")

    async def ensure(self, name):
        return True

    monkeypatch.setattr(EngineStore, "ensure", ensure)
    monkeypatch.setattr(EngineStore, "binary", lambda self: Path(executable))
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    with signed_in_client(app) as client:
        prefix = f"/api/projects/{app.state.default_project_id}/paper"
        sources = {
            "paper/article.tex": r"""\documentclass{article}
\begin{document}
A reference \cite{knuth1984}.
\bibliographystyle{plain}
\bibliography{references}
\end{document}
""",
            "paper/references.bib": """@book{knuth1984,
  author = {Donald E. Knuth},
  title = {The TeXbook},
  publisher = {Addison-Wesley},
  year = {1984}
}
""",
        }
        ids = {}
        for path, content in sources.items():
            response = client.post(
                prefix + "/uploads", data={"path": path}, files={"file": (Path(path).name, content)}
            )
            assert response.status_code == 200, response.text
            ids[path] = response.json()["file_ids"][0]
        response = client.get(
            prefix + "/export/arxiv", params={"file_id": ids["paper/article.tex"]}
        )
        assert response.status_code == 200, response.text
        unpacked = tmp_path / "unpacked"
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert set(archive.namelist()) == {*sources, "paper/article.bbl"}
            assert b"knuth1984" in archive.read("paper/article.bbl")
            archive.extractall(unpacked)
        output = tmp_path / "offline"
        output.mkdir()
        result = subprocess.run(
            [
                executable,
                "-X",
                "compile",
                "--untrusted",
                "--only-cached",
                "--outdir",
                str(output),
                str(unpacked / "paper/article.tex"),
            ],
            cwd=unpacked,
            env={**os.environ, "TECTONIC_CACHE_DIR": str(app.state.paper_compiler.cache_dir)},
            capture_output=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert (output / "article.pdf").read_bytes().startswith(b"%PDF-")


@pytest.mark.asyncio
async def test_cancelled_export_drains_materialization_before_removing_tree(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    written = threading.Event()
    destinations = []

    class BlockingLive(Live):
        def materialize(self, snapshot, dest):
            destinations.append(dest)
            entered.set()
            if not release.wait(timeout=10):
                raise TimeoutError
            super().materialize(snapshot, dest)
            written.set()

    async def ensure(name):
        return True

    engines = EngineStore(tmp_path)
    monkeypatch.setattr(engines, "ensure", ensure)
    compiler = PaperCompiler(BlockingLive(), engines, tmp_path)
    export = asyncio.create_task(compiler.bibliography("project", "paper.tex"))
    try:
        await async_wait_until(entered.is_set)
        export.cancel()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(export), timeout=0.05)
        assert destinations[0].is_dir()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await export
    assert written.is_set()
    assert not destinations[0].exists()
