"""Wake inputs one storage transaction writes, handed to the store's owner after commit.

A mailbox item or a Bulletin mention may wake an idle recipient. The wake must not
run inside the writing transaction, and a rolled-back write must not wake anyone,
so the writer queues the input on its connection and ``AppStore.connection``
releases it only after that connection commits.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

WakeInputKind = Literal["chat_mail", "bulletin_mention"]

_PENDING: dict[int, list[tuple[str, WakeInputKind, str]]] = {}


def queue_wake_input(
    connection: sqlite3.Connection, project_id: str, kind: WakeInputKind, ref: str
) -> None:
    pending = _PENDING.setdefault(id(connection), [])
    if (project_id, kind, ref) not in pending:
        pending.append((project_id, kind, ref))


def take_wake_inputs(connection: sqlite3.Connection) -> list[tuple[str, WakeInputKind, str]]:
    return _PENDING.pop(id(connection), [])


def wake_caused_sql(task: str) -> str:
    """A turn some wake started: a chat mailbox or mention wake, or an Auto-research wake."""
    return (
        f"(json_extract({task}.request_json,'$.chat_wake') IS NOT NULL "
        f"OR json_extract({task}.request_json,'$.wake_cause') IS NOT NULL)"
    )
