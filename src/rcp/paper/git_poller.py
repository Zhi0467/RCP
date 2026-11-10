"""Poll Git only while a Paper editor has a live subscriber."""

from __future__ import annotations

import logging
import threading
import time
from contextlib import nullcontext

from rcp.limits import PAPER_GIT_POLL_SECONDS, PAPER_GIT_RETRY_SECONDS


class PaperGitPoller:
    def __init__(self, sync, *, admission=None, startup_effect_fence=None):
        self.sync = sync
        self.admission = admission
        self.startup_effect_fence = startup_effect_fence
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._poll_lock = threading.Lock()
        self._retry: dict[str, float] = {}

    def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="rcp-paper-git", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float | None = None) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=PAPER_GIT_POLL_SECONDS if timeout is None else timeout)
            if not self._thread.is_alive():
                self._thread = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def poll_once(self) -> None:
        if self.startup_effect_fence is not None and self.startup_effect_fence.active:
            return
        if self.admission is not None and self.admission.closed:
            return
        with (
            self._poll_lock,
            self.admission.mutation("Paper Git polling") if self.admission else nullcontext(),
        ):
            projects = self.sync.live.run_sync(self.sync.live.open_projects())
            for link in self.sync.store.paper_git_links():
                if self._stop.is_set():
                    break
                if (
                    link.state != "linked"
                    or link.project_id not in projects
                    or self._retry.get(link.project_id, 0) > time.monotonic()
                ):
                    continue
                try:
                    self.sync.pull(link.project_id)
                    self._retry.pop(link.project_id, None)
                except Exception:
                    self._retry[link.project_id] = time.monotonic() + PAPER_GIT_RETRY_SECONDS
                    logging.getLogger(__name__).exception("Paper Git poll failed")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                logging.getLogger(__name__).exception("Paper Git polling failed")
            self._stop.wait(PAPER_GIT_POLL_SECONDS)
