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
    from rcp.storage.models import ProjectRecord
    from rcp.transport.run_stage import RemoteRunStage


class PaperFolder(Protocol):
    remote: bool

    def scan(self) -> list[Entry]: ...

    def read(self, paths: list[str]) -> dict[str, bytes | None]: ...

    def write(self, path: str, data: bytes, expect_sha: str | None) -> WriteResult: ...

    def delete(self, path: str, expect_sha: str | None) -> WriteResult: ...

    def rename(self, source: str, destination: str) -> None: ...

    def install(self, files: dict[str, bytes]) -> bool: ...


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

    def install(self, files: dict[str, bytes]) -> bool:
        return folder_io.install(self.root, files)


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

    def install(self, files: dict[str, bytes]) -> bool:
        encoded = {path: base64.b64encode(data).decode("ascii") for path, data in files.items()}
        return self._call("install", files=encoded)["installed"]


def paper_folder_on(
    data_dir: Path, project_id: str, machine_id: str, host_of: Callable[[str], str]
) -> PaperFolder:
    """The folder of one project whose state lives on `machine_id`."""
    from rcp.machine_cards import BACKEND_MACHINE_ID
    from rcp.transport.run_stage import RemoteRunStage

    if machine_id != BACKEND_MACHINE_ID:
        return RemotePaperFolder(RemoteRunStage(host_of(machine_id)), project_id)
    return LocalPaperFolder(folder_io.paper_root(data_dir, project_id))


def paper_folders(store: AppStore, data_dir: Path) -> Callable[[str], PaperFolder]:
    """The folder on each project's state machine, where its agents run."""
    from rcp.machine_cards import BACKEND_MACHINE_ID, host_of

    def folder(project_id: str) -> PaperFolder:
        project = store.project(project_id)
        machine = project.state_machine_id if project is not None else BACKEND_MACHINE_ID
        return paper_folder_on(data_dir, project_id, machine, lambda card: host_of(store, card))

    return folder


def read_paper_folder(folder: PaperFolder) -> dict[str, bytes]:
    """Every supported file's bytes; a file that vanishes between scan and read is skipped."""
    paths = [entry.path for entry in folder.scan() if entry.kind == "file"]
    return {path: data for path, data in folder.read(paths).items() if data is not None}


def change_project_paper(store: AppStore, project: ProjectRecord, destination: str | None) -> None:
    """Move or delete a project's Paper folder on its state card, as memory does."""
    from rcp.machine_cards import BACKEND_MACHINE_ID, host_of
    from rcp.transport.run_stage import RemoteRunStage

    if project.state_machine_id != BACKEND_MACHINE_ID:
        RemoteRunStage(host_of(store, project.state_machine_id)).paper_folder(
            {"op": "change", "project_id": project.project_id, "destination": destination}
        )
    else:
        folder_io.change_project_folder(store.path.parent, project.project_id, destination)
