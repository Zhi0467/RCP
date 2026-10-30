from __future__ import annotations

import asyncio
import fcntl
import os
import threading
from pathlib import Path

import pytest

from rcp.paper import PaperSnapshot
from rcp.paper.compile import PaperCompiler, root_candidates
from rcp.paper.engines import EngineStore
from rcp.paper.models import PaperFile
from rcp.paper.synctex import synctex_map
from tests.helpers import async_wait_until


@pytest.mark.parametrize(
    ("files", "opened", "expected"),
    [
        (
            {"a.tex": r"\documentclass{article}\input{part}", "part.tex": "text"},
            "part.tex",
            ["a.tex"],
        ),
        (
            {
                "a.tex": r"\documentclass{article}\include{part}",
                "b.tex": r"\documentclass{article}\subfile{part}",
                "part.tex": "text",
            },
            "part.tex",
            ["a.tex", "b.tex"],
        ),
        ({"part.tex": "text"}, "part.tex", []),
        ({"a.tex": "% \\documentclass{article}\ntext"}, "a.tex", []),
        (
            {"a.tex": r"\documentclass{article}\input{sub/b}", "sub/b.tex": r"\input{../a}"},
            "sub/b.tex",
            ["a.tex"],
        ),
        ({"a.tex": r"\documentclass{article}"}, "missing.tex", []),
        (
            {"a.tex": "\\documentclass{article}\n\\input part\n", "part.tex": "text"},
            "part.tex",
            ["a.tex"],
        ),
        (
            {
                "paper/root.tex": r"\documentclass{article}\input{sections/a}",
                "paper/sections/a.tex": r"\include{sections/b}",
                "paper/sections/b.tex": "Nested section",
            },
            "paper/sections/b.tex",
            ["paper/root.tex"],
        ),
        (
            {
                "paper/root.tex": r"\documentclass{article}\subfile{sections/a}",
                "paper/sections/a.tex": r"\input{b}",
                "paper/sections/b.tex": "Nested section",
            },
            "paper/sections/b.tex",
            ["paper/root.tex"],
        ),
    ],
)
def test_latex_roots(files, opened, expected):
    assert root_candidates(files, opened) == expected


class Live:
    def __init__(self):
        self.revision = 1

    async def snapshot(self, project_id, revision=None):
        return PaperSnapshot(
            revision=revision or self.revision,
            files=[
                PaperFile(
                    file_id="file",
                    path="paper.tex",
                    kind="text",
                    text=r"\documentclass{article}\begin{document}Hello\end{document}",
                )
            ],
        )

    def materialize(self, snapshot, dest):
        for entry in snapshot.files:
            (dest / entry.path).write_text(entry.text)


class Engines:
    def __init__(self, executable):
        self.executable = executable

    async def ensure(self, name):
        return True

    def binary(self):
        return self.executable


def fake_compiler(tmp_path, body):
    executable = tmp_path / "tectonic"
    executable.write_text("#!/usr/bin/env python3\n" + body)
    executable.chmod(0o755)
    return Engines(executable)


class SubfileLive(Live):
    async def snapshot(self, project_id, revision=None):
        texts = {
            "main.tex": "\\documentclass{article}\\begin{document}\\subfile{section}\\end{document}",
            "section.tex": "\\documentclass[main]{subfiles}\\begin{document}S\\end{document}",
        }
        files = [PaperFile(file_id=p, path=p, kind="text", text=t) for p, t in texts.items()]
        return PaperSnapshot(revision=1, files=files)


class NoEngine:
    async def ensure(self, name):
        return False

    def error(self, name):
        return ""


@pytest.mark.asyncio
async def test_an_open_root_compiles_itself_even_when_another_root_includes_it(tmp_path):
    compiler = PaperCompiler(SubfileLive(), NoEngine(), tmp_path)
    for chosen in (None, "main.tex"):
        result = await compiler.compile("project", "section.tex", root_path=chosen)
        assert (result.status, result.root_path) == ("unavailable", "section.tex")


def test_synctex_maps_each_files_lines_to_their_first_pdf_mark():
    # Recorded from pinned Tectonic 0.17 `--synctex` on the two sources beside it.
    # Page 1 ships while TeX reads two.tex line 1, so its frame carries that line.
    fixture = Path(__file__).parent / "fixtures" / "paper_synctex"
    mapping = synctex_map((fixture / "two_file.synctex").read_text(), Path("/TREE"))
    assert set(mapping) == {"main.tex", "sections/two.tex"}
    first_line, page, y = mapping["sections/two.tex"][0]
    assert (first_line, page) == (1, 2)
    assert 72 < y < 792
    assert [entry[1] for entry in mapping["main.tex"] if entry[0] == 3] == [1]


def test_synctex_marks_are_in_pdf_points():
    # TeX measures 72.27 points to the inch and PDF 72; the preview scales pages in
    # PDF points. Half an inch of offset plus a quarter inch counted in units of
    # two, magnified twice, is two inches: 144 PDF points.
    half_inch = 72.27 * 65536 / 2
    text = (
        "SyncTeX Version:1\nInput:1:/TREE/main.tex\nMagnification:2000\nUnit:2\n"
        f"Y Offset:{half_inch:.0f}\nContent:\n{{1\n[1,1:0,0:0,0,0\n"
        f"h1,2:0,{half_inch / 2:.0f}:0,0,0\n}}1\n"
    )
    assert synctex_map(text, Path("/TREE")) == {"main.tex": [(2, 1, 144.0)]}


@pytest.mark.asyncio
async def test_new_request_cancels_process_group_and_only_new_pdf_survives(tmp_path):
    ready = tmp_path / "ready"
    engines = fake_compiler(
        tmp_path,
        f"""
import fcntl, os, pathlib, sys, time
ready = pathlib.Path({str(ready)!r})
if not ready.exists():
    if os.fork() == 0:
        lock = ready.with_suffix(".lock").open("w")
        fcntl.flock(lock, fcntl.LOCK_EX)
        ready.write_text(str(os.getppid()) + "," + str(os.getpid()))
    time.sleep(60)
out = pathlib.Path(sys.argv[sys.argv.index("--outdir") + 1])
(out / "paper.pdf").write_bytes(b"%PDF-1.4 test")
""",
    )
    compiler = PaperCompiler(Live(), engines, tmp_path)
    first = asyncio.create_task(compiler.compile("project", "file"))
    # write_text creates the file before writing it.
    await async_wait_until(lambda: ready.exists() and "," in ready.read_text())
    leader, _child = map(int, ready.read_text().split(","))
    second = await compiler.compile("project", "file", revision=2)
    assert (await first).status == "superseded"
    assert second.status == "ok"
    assert compiler.pdf("project", second.compile_id).startswith(b"%PDF-")
    with pytest.raises(KeyError):
        compiler.pdf("other", second.compile_id)
    with pytest.raises(ProcessLookupError):
        os.kill(leader, 0)
    # The child held this lock for its whole lifetime; group cancellation releases it.
    with ready.with_suffix(".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    await compiler.close()


@pytest.mark.asyncio
async def test_identical_requests_share_one_compile(tmp_path):
    runs = tmp_path / "runs"
    go = tmp_path / "go"
    engines = fake_compiler(
        tmp_path,
        f"""
import pathlib, sys, time
with pathlib.Path({str(runs)!r}).open("a") as runs:
    runs.write("run\\n")
while not pathlib.Path({str(go)!r}).exists():
    time.sleep(0.01)
out = pathlib.Path(sys.argv[sys.argv.index("--outdir") + 1])
(out / "paper.pdf").write_bytes(b"%PDF-1.4 test")
""",
    )
    compiler = PaperCompiler(Live(), engines, tmp_path)
    first = asyncio.create_task(compiler.compile("project", "file", revision=1))
    await async_wait_until(runs.exists)
    second = asyncio.create_task(compiler.compile("project", "file", revision=1))
    await asyncio.sleep(0)
    go.touch()
    results = await asyncio.gather(first, second)
    assert [result.status for result in results] == ["ok", "ok"]
    assert runs.read_text().count("run") == 1
    await compiler.close()


@pytest.mark.asyncio
async def test_real_pinned_tectonic_compile(tmp_path):
    # Opt in with a disposable, previously installed RCP engine directory.
    location = os.environ.get("RCP_TEST_PAPER_ENGINE_DATA_DIR")
    if not location:
        pytest.skip("No RCP-owned Tectonic installation supplied")
    engines = EngineStore(Path(location))
    try:
        engines.binary()
    except (OSError, ValueError, RuntimeError):
        pytest.skip("RCP's pinned Tectonic is absent")
    compiler = PaperCompiler(Live(), engines, tmp_path)
    result = await compiler.compile("project", "file")
    assert result.status == "ok", result.log_tail
    assert compiler.pdf("project", result.compile_id).startswith(b"%PDF-")
    assert result.synctex["paper.tex"]
    await compiler.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["log", "output", "timeout"])
async def test_compile_limits_kill_job(tmp_path, monkeypatch, mode):
    import rcp.paper.compile as module

    monkeypatch.setattr(module, "PAPER_COMPILE_MAX_OUTPUT_BYTES", 1024)
    monkeypatch.setattr(module, "PAPER_COMPILE_TIMEOUT_SECONDS", 1)
    body = {
        "log": 'import sys, time\nsys.stdout.write("x" * 2048); sys.stdout.flush(); time.sleep(60)',
        "output": 'import pathlib, time\npathlib.Path("generated").write_bytes(b"x" * 2048); time.sleep(60)',
        "timeout": "import time\ntime.sleep(60)",
    }[mode]
    compiler = PaperCompiler(Live(), fake_compiler(tmp_path, body), tmp_path)
    result = await compiler.compile("project", "file")
    assert result.status == "error"
    assert result.log_tail
    with pytest.raises(KeyError):
        compiler.pdf("project", result.compile_id)
    await compiler.close()


@pytest.mark.asyncio
async def test_close_waits_for_materialization_before_removing_tree(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    written = threading.Event()
    destinations = []

    class BlockingLive(Live):
        def materialize(self, snapshot, dest):
            destinations.append(dest)
            entered.set()
            if not release.wait(timeout=10):
                raise TimeoutError("Materialization was not released")
            super().materialize(snapshot, dest)
            written.set()

    compiler = PaperCompiler(BlockingLive(), Engines(tmp_path / "unused"), tmp_path)
    request = asyncio.create_task(compiler.compile("project", "file"))
    closing = None
    try:
        await async_wait_until(entered.is_set)
        closing = asyncio.create_task(compiler.close())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(closing), timeout=0.05)
        assert destinations[0].is_dir()
    finally:
        release.set()
        if closing is not None:
            await closing
        else:
            await compiler.close()
        result = await request
    assert result.status == "superseded"
    assert written.is_set()
    assert not destinations[0].exists()


@pytest.mark.asyncio
async def test_previous_pdf_survives_until_result_limit(tmp_path, monkeypatch):
    monkeypatch.setattr("rcp.paper.compile.PAPER_COMPILE_RESULT_LIMIT", 2)
    compiler = PaperCompiler(Live(), Engines(tmp_path / "unused"), tmp_path)

    async def run(tree, output, root):
        (output / "paper.pdf").write_bytes(b"%PDF-1.7 retained")
        return 0, ""

    monkeypatch.setattr(compiler, "_run", run)
    first = await compiler.compile("p", "file")
    await compiler.compile("p", "missing")
    second = await compiler.compile("p", "file")
    assert compiler.pdf("p", first.compile_id) == compiler.pdf("p", second.compile_id)
    await compiler.compile("p", "file")
    with pytest.raises(KeyError):
        compiler.pdf("p", first.compile_id)
    await compiler.close()
