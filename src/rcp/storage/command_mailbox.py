"""Private restart checkpoints for the owner of a single command-mailbox turn."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path

from rcp.agents.provider_environment import ProviderCredentialStore, _write_private
from rcp.rcp_home import private_directory

logger = logging.getLogger(__name__)

# Background workers run separate event loops, and can construct separate stores.
_CHECKPOINT_LOCK = threading.Lock()


class CommandMailboxStore:
    """Keep turn secrets outside public task records and protected backups.

    The existing provider-credential root supplies the exclusion boundary; the
    existing private writer supplies fsync, atomic replacement, and mode 0600.
    The concrete mailbox owner validates its checkpoint payload on restoration.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> CommandMailboxStore:
        return cls(ProviderCredentialStore.for_data_dir(data_dir).root / "command-mailboxes")

    def _path(self, operation_id: str) -> Path:
        if not operation_id or operation_id != operation_id.strip():
            raise ValueError("command mailbox operation id must be a nonblank exact identifier")
        return self.root / (hashlib.sha256(operation_id.encode()).hexdigest() + ".json")

    def save(self, operation_id: str, payload: dict[str, object]) -> None:
        document = json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n"
        path = self._path(operation_id)
        with _CHECKPOINT_LOCK:
            private_directory(self.root, "command mailbox checkpoints")
            _write_private(path, document)

    def load(self, operation_id: str) -> dict[str, object] | None:
        path = self._path(operation_id)
        with _CHECKPOINT_LOCK:
            if not self.root.exists():
                return None
            private_directory(self.root, "command mailbox checkpoints")
            try:
                document = path.read_text()
            except FileNotFoundError:
                return None
        value = json.loads(document)
        if not isinstance(value, dict):
            raise ValueError("command mailbox checkpoint must be an object")
        return value

    def delete(self, operation_id: str, *, mailbox_id: str | None = None) -> None:
        """Remove a turn's checkpoint; with ``mailbox_id``, only if it still names that mailbox.

        A resumed turn reuses the operation id, so an older owner closing late
        must not delete the newer mailbox's checkpoint.
        """

        path = self._path(operation_id)
        with _CHECKPOINT_LOCK:
            if not self.root.exists():
                return
            private_directory(self.root, "command mailbox checkpoints")
            if mailbox_id is not None:
                try:
                    saved = json.loads(path.read_text())
                except FileNotFoundError:
                    return
                except (OSError, ValueError):
                    saved = None
                if isinstance(saved, dict) and saved.get("mailbox_id") != mailbox_id:
                    return
            path.unlink(missing_ok=True)

    def operation_ids(self) -> list[str]:
        """The turns with readable checkpoints; unreadable ones are logged and deleted."""

        with _CHECKPOINT_LOCK:
            if not self.root.exists():
                return []
            private_directory(self.root, "command mailbox checkpoints")
            paths = sorted(self.root.glob("*.json"))
        found: list[str] = []
        for path in paths:
            task_id = None
            try:
                task_id = (json.loads(path.read_text()).get("identity") or {}).get("task_id")
            except FileNotFoundError:
                continue
            except (OSError, ValueError, AttributeError):
                pass
            if isinstance(task_id, str) and task_id:
                found.append(task_id)
                continue
            # No turn can be named, so none can resume it; its secret must not linger.
            logger.warning("deleting unreadable command mailbox checkpoint %s", path.name)
            with _CHECKPOINT_LOCK:
                path.unlink(missing_ok=True)
        return found
