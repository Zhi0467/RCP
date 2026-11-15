"""Real live Paper owners for synchronous control-worker tests."""

from __future__ import annotations

import asyncio
import threading

from rcp.paper import PaperLive, PaperOp

_owners: list[tuple[PaperLive, asyncio.AbstractEventLoop, threading.Thread]] = []


def start_paper(live: PaperLive) -> PaperLive:
    if live.loop is not None and live.loop.is_running():
        return live
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()
        loop.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ready.wait()
    live.loop = loop
    _owners.append((live, loop, thread))
    return live


def close_paper_owners() -> None:
    while _owners:
        live, loop, thread = _owners.pop()
        live.run_sync(live.close())
        loop.call_soon_threadsafe(loop.stop)
        thread.join()


def saved(live: PaperLive, project: str = "p"):
    return live.run_sync(live.snapshot(project))


def apply(live: PaperLive, ops: list[PaperOp], project: str = "p"):
    return live.run_sync(live.apply(project, ops, "human"))


def create(live: PaperLive, path: str, text: str = "", project: str = "p") -> str:
    op = PaperOp(kind="write", path=path, data=text.encode(), exclusive=True)
    return apply(live, [op], project).file_ids[0]


def human_edit(live: PaperLive, file_id: str, text: str, project: str = "p"):
    file = next(file for file in saved(live, project).files if file.file_id == file_id)
    old = (file.text or "").encode()
    return apply(
        live, [PaperOp(kind="write", path=file.path, base=old, data=text.encode())], project
    )
