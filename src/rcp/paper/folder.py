"""One project's Paper folder on its state machine, local or over SSH."""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from rcp import limits
from rcp.paper import folder_io
from rcp.paper.folder_io import Entry, WriteResult

if TYPE_CHECKING:
    from rcp.storage import AppStore
    from rcp.transport.run_stage import RemoteRunStage


class PaperFolder(Protocol):
    remote: bool

    def scan(self) -> list[Entry]: ...

    def read(self, paths: list[str]) -> dict[str, bytes | None]: ...

    def write(self, path: str, data: bytes, expect_sha: str | None) -> WriteResult: ...

    def delete(self, path: str, expect_sha: str | None) -> WriteResult: ...

    def rename(self, source: str, destination: str) -> None: ...


def _settle(remote: bool) -> dict[str, float]:
    seconds = limits.PAPER_REMOTE_POLL_SECONDS if remote else limits.PAPER_POLL_SECONDS
    return {"settle_seconds": seconds, "settle_max_seconds": seconds}


class LocalPaperFolder:
    remote = False

    def __init__(self, root: Path):
        self.root = root

    def scan(self) -> list[Entry]:
        return folder_io.scan(self.root)

    def read(self, paths: list[str]) -> dict[str, bytes | None]:
        return folder_io.read(self.root, paths)

    def write(self, path: str, data: bytes, expect_sha: str | None) -> WriteResult:
        return folder_io.write(self.root, path, data, expect_sha, **_settle(False))

    def delete(self, path: str, expect_sha: str | None) -> WriteResult:
        return folder_io.delete(self.root, path, expect_sha, **_settle(False))

    def rename(self, source: str, destination: str) -> None:
        folder_io.rename(self.root, source, destination)


def _decode(value: str | None) -> bytes | None:
    return None if value is None else base64.b64decode(value)


def _result(payload: dict) -> WriteResult:
    return WriteResult(
        payload["sha"], _decode(payload["displaced"]), payload["displaced_sha"], payload["conflict"]
    )


class RemotePaperFolder:
    remote = True

    def __init__(self, stage: RemoteRunStage, project_id: str):
        self.stage = stage
        self.project_id = project_id

    def _call(self, op: str, **fields) -> dict:
        return self.stage.paper_folder({"op": op, "project_id": self.project_id, **fields})

    def scan(self) -> list[Entry]:
        return [Entry(**entry) for entry in self._call("scan")["entries"]]

    def read(self, paths: list[str]) -> dict[str, bytes | None]:
        files = self._call("read", paths=paths)["files"]
        return {path: _decode(value) for path, value in files.items()}

    def write(self, path: str, data: bytes, expect_sha: str | None) -> WriteResult:
        encoded = base64.b64encode(data).decode("ascii")
        return _result(
            self._call("write", path=path, data=encoded, expect_sha=expect_sha, **_settle(True))
        )

    def delete(self, path: str, expect_sha: str | None) -> WriteResult:
        return _result(self._call("delete", path=path, expect_sha=expect_sha, **_settle(True)))

    def rename(self, source: str, destination: str) -> None:
        self._call("rename", source=source, destination=destination)


def paper_folders(store: AppStore, data_dir: Path) -> Callable[[str], PaperFolder]:
    """The folder on each project's state machine, where its agents run."""
    from rcp.machine_cards import BACKEND_MACHINE_ID, host_of
    from rcp.transport.run_stage import RemoteRunStage

    def folder(project_id: str) -> PaperFolder:
        project = store.project(project_id)
        if project is not None and project.state_machine_id != BACKEND_MACHINE_ID:
            stage = RemoteRunStage(host_of(store, project.state_machine_id))
            return RemotePaperFolder(stage, project_id)
        return LocalPaperFolder(folder_io.paper_root(data_dir, project_id))

    return folder
