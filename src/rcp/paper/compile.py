"""Bounded previews of saved paper trees; no canonical state is changed."""

from __future__ import annotations

import asyncio
import contextlib
import os
import posixpath
import re
import signal
import tempfile
import uuid
from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel

from rcp.limits import (
    PAPER_COMPILE_CONCURRENCY,
    PAPER_COMPILE_MAX_LOG_BYTES,
    PAPER_COMPILE_MAX_OUTPUT_BYTES,
    PAPER_COMPILE_POLL_SECONDS,
    PAPER_COMPILE_RESULT_LIMIT,
    PAPER_COMPILE_TIMEOUT_SECONDS,
)
from rcp.paper.engines import EngineStore
from rcp.paper.models import PaperSnapshot
from rcp.paper.synctex import SyncTexMap, read_synctex


class PaperLiveAdapter(Protocol):
    """The live owner snapshots on its loop; materialization is worker-safe."""

    async def snapshot(self, project_id: str, revision: int | None = None) -> PaperSnapshot: ...

    def materialize(self, snapshot: PaperSnapshot, dest: Path) -> None: ...


class CompileResult(BaseModel):
    compile_id: str
    status: Literal["ok", "error", "superseded", "unavailable"]
    root_path: str | None = None
    candidates: list[str] | None = None
    log_tail: str = ""
    pdf_url: str | None = None
    # Per tree path, each source line's first PDF mark: [line, page, y points from top].
    synctex: SyncTexMap = {}


def _reference(source: str, target: str, suffix: str) -> str | None:
    if target.startswith(("/", "@")):
        return None
    path = posixpath.normpath(posixpath.join(posixpath.dirname(source), target))
    if path == ".." or path.startswith("../"):
        return None
    return path if posixpath.splitext(path)[1] else path + suffix


def root_candidates(files: Mapping[str, str], open_path: str) -> list[str]:
    """Find static LaTeX roots reaching the open file; traverse cycles once.

    An open file that is itself a root compiles alone, even when another root
    also includes it: the preview shows the file the writer is in.
    """
    suffix = ".tex"
    texts = {path: text for path, text in files.items() if path.endswith(suffix)}
    roots: set[str] = set()
    latex_refs: dict[str, list[tuple[str, str]]] = {}
    for path, text in texts.items():
        text = re.sub(r"(?<!\\)%[^\n]*", "", text)
        if re.search(r"\\documentclass\s*(?:\[[^\]]*\])?\s*\{", text):
            roots.add(path)
        latex_refs[path] = [
            (command, braced or bare)
            for command, braced, bare in re.findall(
                r"\\(input|include|subfile)(?![a-zA-Z])\s*(?:\{([^}]+)\}|([^\s{}\\]+))", text
            )
        ]

    def reaches(root: str) -> bool:
        pending: list[tuple[str, str]] = [(root, root)]
        visited: set[tuple[str, str]] = set()
        while pending:
            node, context = pending.pop()
            if node == open_path:
                return True
            if (node, context) in visited:
                continue
            visited.add((node, context))
            # Tectonic roots ordinary TeX I/O at the primary input's parent.
            # A subfile introduces its own directory for included content.
            for command, target in latex_refs.get(node, ()):
                child = _reference(context, target, suffix)
                if child is not None and child in texts:
                    pending.append((child, child if command == "subfile" else context))
        return False

    if open_path in roots:
        return [open_path]
    return sorted(root for root in roots if reaches(root))


async def _kill(process: asyncio.subprocess.Process) -> None:
    # The group can contain BibTeX/other children even after the leader exits.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    await process.wait()


_Request = tuple[str, int | None, str | None]


class PaperCompiler:
    """One event-loop owner, one newest job per project, globally bounded workers."""

    def __init__(self, live: PaperLiveAdapter, engines: EngineStore, data_dir: Path):
        self.live = live
        self.engines = engines
        self.cache_dir = data_dir / "paper-engines" / "tectonic-cache"
        self._slots = asyncio.Semaphore(PAPER_COMPILE_CONCURRENCY)
        self._jobs: dict[str, tuple[_Request, asyncio.Task[CompileResult]]] = {}
        self._waiters: dict[asyncio.Task[CompileResult], int] = {}
        self._pdfs: OrderedDict[str, tuple[str, bytes]] = OrderedDict()

    async def compile(
        self,
        project_id: str,
        file_id: str,
        revision: int | None = None,
        root_path: str | None = None,
    ) -> CompileResult:
        compile_id = uuid.uuid4().hex
        request = (file_id, revision, root_path)
        running = self._jobs.get(project_id)
        # Viewers of one saved revision ask for the same PDF: they share one job.
        if running is not None and running[0] == request:
            job = running[1]
        else:
            if running is not None:
                running[1].cancel()
            job = asyncio.create_task(
                self._compile(project_id, file_id, revision, root_path, compile_id)
            )
            self._jobs[project_id] = (request, job)
        self._waiters[job] = self._waiters.get(job, 0) + 1
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            if job.cancelled():
                return CompileResult(compile_id=compile_id, status="superseded")
            if self._waiters[job] == 1:
                job.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await job
            raise
        finally:
            self._waiters[job] -= 1
            if not self._waiters[job]:
                del self._waiters[job]
                if self._jobs.get(project_id, (None, None))[1] is job:
                    del self._jobs[project_id]

    async def _compile(
        self,
        project_id: str,
        file_id: str,
        revision: int | None,
        root_path: str | None,
        compile_id: str,
    ) -> CompileResult:
        result = CompileResult(compile_id=compile_id, status="error")
        try:
            snapshot = await self.live.snapshot(project_id, revision)
            entries = snapshot.files
            open_path = next((f.path for f in entries if f.file_id == file_id), None)
            if open_path is None:
                raise ValueError("The open file is absent from this saved revision.")
            texts = {f.path: f.text for f in entries if f.kind == "text" and f.text is not None}
            roots = root_candidates(texts, open_path)
            # A chosen root must still be one that reaches the open file.
            if root_path in roots:
                roots = [root_path]
            if len(roots) != 1:
                result.candidates = roots
                result.log_tail = "No unique LaTeX root reaches the open file."
                return result
            result.root_path = roots[0]
            if not await self.engines.ensure("latex"):
                result.status = "unavailable"
                result.log_tail = self.engines.error("latex")
                return result
            async with self._slots:
                with tempfile.TemporaryDirectory(prefix="rcp-paper-") as temporary:
                    tree = Path(temporary) / "source"
                    tree.mkdir()
                    materialization = asyncio.create_task(
                        asyncio.to_thread(self.live.materialize, snapshot, tree)
                    )
                    try:
                        await asyncio.shield(materialization)
                    except asyncio.CancelledError:
                        # Cancellation cannot stop the writer thread. Keep its
                        # temporary tree alive and drain it before releasing ownership.
                        while not materialization.done():
                            with contextlib.suppress(asyncio.CancelledError, Exception):
                                await asyncio.shield(materialization)
                        with contextlib.suppress(Exception):
                            materialization.result()
                        raise
                    output = Path(temporary) / "output"
                    output.mkdir()
                    self.cache_dir.mkdir(parents=True, exist_ok=True)
                    code, log = await self._run(tree, output, roots[0])
                    result.log_tail = log
                    pdf = output / (Path(roots[0]).stem + ".pdf")
                    if code == 0 and pdf.is_file() and not pdf.is_symlink():
                        if pdf.stat().st_size > PAPER_COMPILE_MAX_OUTPUT_BYTES:
                            raise ValueError("Compiled PDF exceeds the output limit.")
                        data = pdf.read_bytes()
                        if not data.startswith(b"%PDF-"):
                            raise ValueError("Compiler did not produce a PDF.")
                        self._pdfs[compile_id] = (project_id, data)
                        synctex = output / (Path(roots[0]).stem + ".synctex.gz")
                        if synctex.is_file() and not synctex.is_symlink():
                            mapping = await asyncio.to_thread(read_synctex, synctex, tree)
                            if mapping is None:
                                result.log_tail += "\nSyncTeX data exceeds its limit."
                            else:
                                result.synctex = mapping
                        while len(self._pdfs) > PAPER_COMPILE_RESULT_LIMIT:
                            self._pdfs.popitem(last=False)
                        result.status = "ok"
                        result.pdf_url = (
                            f"/api/projects/{project_id}/paper/compile/{compile_id}/pdf"
                        )
        except TimeoutError:
            result.log_tail = "Compile exceeded the wall-time limit."
        except (OSError, ValueError) as exc:
            result.log_tail = str(exc)[-PAPER_COMPILE_MAX_LOG_BYTES:]
        return result

    async def _run(self, tree: Path, output: Path, root: str) -> tuple[int, str]:
        source_sizes = {p: p.stat().st_size for p in tree.rglob("*") if p.is_file()}
        # Root is an absolute positional argument: paper names cannot become flags.
        launch = asyncio.create_task(
            asyncio.create_subprocess_exec(
                str(self.engines.binary()),
                "-X",
                "compile",
                "--untrusted",
                "--synctex",
                "--outdir",
                str(output),
                str(tree / root),
                cwd=tree,
                env={**os.environ, "TECTONIC_CACHE_DIR": str(self.cache_dir)},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        )
        try:
            process = await asyncio.shield(launch)
        except asyncio.CancelledError:
            process = await launch
            await _kill(process)
            raise
        tail = bytearray()

        def check_output() -> None:
            size = sum(p.stat().st_size for p in output.rglob("*") if p.is_file())
            size += sum(
                max(0, p.stat().st_size - source_sizes.get(p, 0))
                for p in tree.rglob("*")
                if p.is_file()
            )
            if size > PAPER_COMPILE_MAX_OUTPUT_BYTES:
                raise ValueError("Compile output exceeds the output limit.")

        async def read_log() -> None:
            assert process.stdout is not None
            total = 0
            while chunk := await process.stdout.read(8192):
                total += len(chunk)
                if total > PAPER_COMPILE_MAX_OUTPUT_BYTES:
                    raise ValueError("Compile log exceeds the output limit.")
                tail.extend(chunk)
                del tail[:-PAPER_COMPILE_MAX_LOG_BYTES]

        async def watch_output() -> None:
            while True:
                check_output()
                await asyncio.sleep(PAPER_COMPILE_POLL_SECONDS)

        reader = asyncio.create_task(read_log())
        watcher = asyncio.create_task(watch_output())
        waiter = asyncio.create_task(process.wait())
        try:
            async with asyncio.timeout(PAPER_COMPILE_TIMEOUT_SECONDS):
                pending: set[asyncio.Task[int] | asyncio.Task[None]] = {waiter, watcher, reader}
                while True:
                    done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        await task
                    if waiter in done:
                        break
                    pending.difference_update(done)
                await reader
                check_output()
                return await waiter, tail.decode("utf-8", errors="replace")
        finally:
            await _kill(process)
            for task in (reader, watcher, waiter):
                task.cancel()
            await asyncio.gather(reader, watcher, waiter, return_exceptions=True)

    def pdf(self, project_id: str, compile_id: str) -> bytes:
        owner, data = self._pdfs[compile_id]
        if owner != project_id:
            raise KeyError(compile_id)
        return data

    async def close(self) -> None:
        jobs = [job for _request, job in self._jobs.values()]
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        self._pdfs.clear()
