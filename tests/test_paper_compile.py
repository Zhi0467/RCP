from __future__ import annotations

import asyncio
import fcntl
import os
import threading
from pathlib import Path

import pytest

from rcp.paper import PaperSnapshot
from rcp.paper.compile import PaperCompiler, root_candidates, typst_root_candidates
from rcp.paper.engines import EngineStore
from rcp.paper.models import PaperFile
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
    ],
)
def test_latex_roots(files, opened, expected):
    assert root_candidates(files, opened, "latex") == expected


def test_typst_roots_include_import_comments_and_cycles():
    files = {
        "a.typ": '#import "sub/b.typ": *',
        "sub/b.typ": '#include "c.typ"',
        "sub/c.typ": "text",
    }
    assert typst_root_candidates(files, "sub/c.typ") == ["a.typ"]
    files["other.typ"] = '#include "sub/c.typ"'
    assert typst_root_candidates(files, "sub/c.typ") == ["a.typ", "other.typ"]
    files["other.typ"] = '// #include "sub/c.typ"'
    files["sub/c.typ"] = '#include "../a.typ"'
    assert typst_root_candidates(files, "sub/c.typ") == []


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
    await async_wait_until(ready.exists)
    leader, _child = map(int, ready.read_text().split(","))
    second = await compiler.compile("project", "file")
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
async def test_stale_saved_revision_discards_pdf(tmp_path):
    live = Live()
    engines = fake_compiler(
        tmp_path,
        """
import pathlib, sys
out = pathlib.Path(sys.argv[sys.argv.index("--outdir") + 1])
(out / "paper.pdf").write_bytes(b"%PDF-1.4 test")
""",
    )
    compiler = PaperCompiler(live, engines, tmp_path)
    result = await compiler.compile("project", "file", revision=2)
    assert result.status == "superseded"
    with pytest.raises(KeyError):
        compiler.pdf("project", result.compile_id)


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
    await compiler.close()


def test_unbraced_latex_input():
    assert root_candidates(
        {"a.tex": "\\documentclass{article}\n\\input part\n", "part.tex": "text"},
        "part.tex",
        "latex",
    ) == ["a.tex"]


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


def test_latex_nested_inputs_resolve_from_document_root():
    files = {
        "paper/root.tex": r"\documentclass{article}\input{sections/a}",
        "paper/sections/a.tex": r"\include{sections/b}",
        "paper/sections/b.tex": "Nested section",
    }
    assert root_candidates(files, "paper/sections/b.tex", "latex") == ["paper/root.tex"]
    files["paper/root.tex"] = r"\documentclass{article}\subfile{sections/a}"
    files["paper/sections/a.tex"] = r"\input{b}"
    assert root_candidates(files, "paper/sections/b.tex", "latex") == ["paper/root.tex"]


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
