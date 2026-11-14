"""A Paper chat's turns: the project's Paper folder and one running Paper turn per project.

A Paper turn launches only when it is its project's oldest unsettled Paper turn.
The rest stay queued and launch, oldest first, when the running one settles.
The editor holds the Paper for a turn from just before launch until its provider
process has exited or recovery settled it; ending it records the `paper_turn`
receipt.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from rcp.agents.memory_file_io import safe_component
from rcp.paper.folder_io import paper_root
from rcp.transport.run_stage import RemoteRunStage

if TYPE_CHECKING:
    from rcp.background import BackgroundAgentTasks
    from rcp.paper import PaperLive
    from rcp.storage import AppStore
    from rcp.storage.models import AgentTaskRecord

logger = logging.getLogger(__name__)

PAPER_TURN_WAIT_MESSAGE = "Waiting for another Paper turn."


def paper_root_for(store: AppStore, project_id: str, remote_stage: RemoteRunStage | None) -> str:
    """Create the project's Paper folder on the machine the turn runs on and return it."""
    if remote_stage is not None:
        return remote_stage.paper_folder({"op": "root", "project_id": project_id})["root"]
    return str(paper_root(store.path.parent.expanduser().resolve(), project_id))


def paper_folder_path(store: AppStore, project_id: str, *, remote: bool) -> str:
    """The Paper folder as a read-only pointer names it; nothing is created."""
    safe_component(project_id)
    if remote:
        return f"~/.rcp/paper/{project_id}"
    return str(Path(store.path.parent).expanduser().resolve() / "paper" / project_id)


def is_paper_turn(record: AgentTaskRecord) -> bool:
    authority = record.dispatch_authority
    return authority is not None and authority.scope.focus == "paper"


_UNSETTLED = ("queued", "running", "pausing")


def _oldest_unsettled_paper_turn(store: AppStore, project_id: str) -> tuple[str, str] | None:
    with store.connection() as connection:
        row = connection.execute(
            "SELECT operation_id, status FROM graph_runs WHERE project_id=? "
            "AND status IN (?,?,?) "
            "AND json_extract(dispatch_authority_json,'$.scope.focus')='paper' "
            "ORDER BY rowid LIMIT 1",
            (project_id, *_UNSETTLED),
        ).fetchone()
    return (str(row[0]), str(row[1])) if row is not None else None


def paper_turn_waits(store: AppStore, record: AgentTaskRecord) -> bool:
    """Whether an older Paper turn of the project still holds the gate; marks the wait."""
    if not is_paper_turn(record):
        return False
    holder = _oldest_unsettled_paper_turn(store, record.project_id)
    if holder is None or holder[0] == record.operation_id:
        return False
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET status_message=?, updated_at=? "
            "WHERE operation_id=? AND status='queued'",
            (PAPER_TURN_WAIT_MESSAGE, store.now(), record.operation_id),
        )
    return True


def wake_paper_turns(background: BackgroundAgentTasks, record: AgentTaskRecord) -> None:
    """After a Paper turn settles, launch the project's oldest waiting one."""
    if not is_paper_turn(record):
        return
    holder = _oldest_unsettled_paper_turn(background.store, record.project_id)
    if holder is not None and holder[1] == "queued":
        background.launch_admitted(holder[0])


def end_paper_turn(store: AppStore, live: PaperLive, project_id: str, operation_id: str) -> None:
    """Release the Paper from a turn whose provider is gone and record its change list."""
    try:
        turn = live.run_sync(live.end_turn(project_id, operation_id))
    except KeyError:
        return
    store.record_agent_task_receipt(operation_id, "paper_turn", turn)


def end_settled_paper_turns(
    store: AppStore, live: PaperLive, project_id: str | None = None
) -> None:
    """End every turn still holding a Paper although its task has settled."""
    with store.connection() as connection:
        rows = connection.execute(
            "SELECT p.project_id, p.turn_operation_id FROM paper_state p "
            "LEFT JOIN graph_runs g ON g.operation_id=p.turn_operation_id "
            "WHERE p.turn_operation_id IS NOT NULL AND (? IS NULL OR p.project_id=?) "
            "AND (g.status IS NULL OR g.status NOT IN (?,?,?))",
            (project_id, project_id, *_UNSETTLED),
        ).fetchall()
    for project, operation_id in rows:
        try:
            end_paper_turn(store, live, str(project), str(operation_id))
        except Exception:
            logger.exception("Paper turn %s could not be ended", operation_id)


def begin_paper_turn(background: BackgroundAgentTasks, record: AgentTaskRecord) -> None:
    """Hold the Paper for a launching Paper turn, after any settled holder lets go."""
    live = background.paper_live
    if live is None or not is_paper_turn(record):
        return
    end_settled_paper_turns(background.store, live, record.project_id)
    live.run_sync(live.begin_turn(record.project_id, record.operation_id))


def finish_paper_turn(background: BackgroundAgentTasks, record: AgentTaskRecord) -> None:
    """After a Paper turn settles: release its hold, then launch the next waiting turn."""
    try:
        if background.paper_live is not None and is_paper_turn(record):
            end_paper_turn(
                background.store, background.paper_live, record.project_id, record.operation_id
            )
    finally:
        wake_paper_turns(background, record)
