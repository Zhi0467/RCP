"""A Paper chat's turns: the project's Paper folder and one running Paper turn per project.

A Paper turn launches only when it is its project's oldest unsettled Paper turn.
The rest stay queued and launch, oldest first, when the running one settles.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from rcp.agents.memory_file_io import ensure_directory, safe_component
from rcp.transport.run_stage import RemoteRunStage

if TYPE_CHECKING:
    from rcp.background import BackgroundAgentTasks
    from rcp.storage import AppStore
    from rcp.storage.models import AgentTaskRecord

PAPER_TURN_WAIT_MESSAGE = "Waiting for another Paper turn."

_REMOTE_PAPER_ROOT = (
    "import os,sys\n"
    "root=os.path.join(os.path.expanduser('~'),'.rcp','paper',sys.argv[1])\n"
    "os.makedirs(root,mode=0o700,exist_ok=True)\n"
    "print(root)\n"
)


def paper_root_for(store: AppStore, project_id: str, remote_stage: RemoteRunStage | None) -> str:
    """Create the project's Paper folder on the machine the turn runs on and return it."""
    safe_component(project_id)
    if remote_stage is not None:
        result = remote_stage._ssh(["python3", "-c", _REMOTE_PAPER_ROOT, project_id])
        if result.returncode:
            raise ValueError(result.stderr.strip() or "could not prepare the remote Paper folder")
        return result.stdout.strip()
    root = store.path.parent.expanduser().resolve() / "paper" / project_id
    ensure_directory(root)
    return str(root)


def paper_folder_path(store: AppStore, project_id: str, *, remote: bool) -> str:
    """The Paper folder as a read-only pointer names it; nothing is created."""
    safe_component(project_id)
    if remote:
        return f"~/.rcp/paper/{project_id}"
    return str(Path(store.path.parent).expanduser().resolve() / "paper" / project_id)


def is_paper_turn(record: AgentTaskRecord) -> bool:
    authority = record.dispatch_authority
    return authority is not None and authority.scope.focus == "paper"


def _oldest_unsettled_paper_turn(store: AppStore, project_id: str) -> tuple[str, str] | None:
    with store.connection() as connection:
        row = connection.execute(
            "SELECT operation_id, status FROM graph_runs WHERE project_id=? "
            "AND status IN ('queued','running','pausing') "
            "AND json_extract(dispatch_authority_json,'$.scope.focus')='paper' "
            "ORDER BY rowid LIMIT 1",
            (project_id,),
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
