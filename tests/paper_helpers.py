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


def human_edit(live: PaperLive, file_id: str, text: str, project: str = "p"):
    old = next(file.text or "" for file in saved(live, project).files if file.file_id == file_id)
    return live.run_sync(
        live.apply(
            project,
            [PaperOp(kind="replace", file_id=file_id, start=0, end=len(old), text=text)],
            "human",
            {"checks": [{"file_id": file_id, "text": old}]},
        )
    )


def receipts(live: PaperLive):
    import json

    with live.store.connection() as connection:
        return [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT payload FROM paper_change_receipts WHERE json_extract(payload,'$.receipt') IS NOT NULL"
            )
        ]


def wire_saved_paper(service):
    live = start_paper(service.paper_live)
    project = service.paper.project_id
    snapshot = saved(live, project)
    if snapshot.files:
        apply(live, [PaperOp(kind="delete", file_id=f.file_id) for f in snapshot.files], project)
    apply(
        live,
        [PaperOp(kind="create", file_id="paper-file", path="paper.md", text="Saved paper text.\n")],
        project,
    )
    return live
