from __future__ import annotations

import codecs
import os
import stat
from pathlib import Path

from rcp.config import Manifest
from rcp.storage import AppStore
from rcp.transport import LocalStateWorkspace, StateWorkspace


def validate_canonical_introduction_backup(path: Path) -> None:
    """Validate one copied introduction as UTF-8 Markdown without loading it whole."""

    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("The copied Paper introduction is not a regular file.")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            if "\x00" in decoder.decode(chunk):
                raise ValueError("The copied Paper introduction contains NUL text.")
        if "\x00" in decoder.decode(b"", final=True):
            raise ValueError("The copied Paper introduction contains NUL text.")
    except UnicodeDecodeError as exc:
        raise ValueError("The copied Paper introduction is not UTF-8 Markdown.") from exc
    finally:
        os.close(descriptor)


class PaperService:
    def __init__(
        self,
        manifest: Manifest,
        store: AppStore,
        workspace: StateWorkspace | None = None,
        *,
        project_id: str | None = None,
    ) -> None:
        self.manifest = manifest
        self.store = store
        self.workspace = workspace or LocalStateWorkspace(
            manifest.research_dir, str(manifest.research_dir)
        )
        self.project_id = project_id or manifest.name
        self.canonical_path = self.workspace.root / "paper" / "introduction.md"

    def restore_canonical(
        self,
        source: Path,
        *,
        expected_sha256: str,
        expected_size: int,
    ) -> None:
        """Validate and publish the archived introduction without changing its bytes."""

        validate_canonical_introduction_backup(source)
        self.workspace.restore_exact_file(
            Path("paper/introduction.md"),
            source,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
        )
