"""Agent identity and each agent's current provider session.

An agent is one id. `agents` holds its placement: who sees it and where it
works. `agent_sessions` names the one provider session its next turn continues,
and the turn that bound it. A session row is replaced only through
`bind_session`, which hands the replaced session back so the caller records why.

An agent's title and archive state stay with `chat_display`, their one writer;
`agent` reads them through.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Collection
from typing import Literal

from pydantic import BaseModel

from rcp.storage.mixin_base import StoreMixinBase


class AgentRecord(BaseModel):
    agent_id: str
    space_id: str
    project_id: str | None = None
    visibility: Literal["private", "shared"]
    entry: Literal["single_entry", "project"]
    owner_user_id: str | None = None
    created_at: str
    archived_at: str | None = None
    title: str | None = None


class AgentSessionRecord(BaseModel):
    agent_id: str
    project_id: str
    kind: str
    provider: str
    machine_id: str
    native_session_id: str
    bound_at: str
    operation_id: str


_SPACE_ID = "(SELECT space_id FROM space_identity WHERE singleton=1)"


def migrate_agents(connection: sqlite3.Connection, session_task_kinds: Collection[str]) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS agents (
        agent_id TEXT PRIMARY KEY, space_id TEXT NOT NULL, project_id TEXT,
        visibility TEXT NOT NULL CHECK(visibility IN ('private', 'shared')),
        entry TEXT NOT NULL CHECK(entry IN ('single_entry', 'project')),
        owner_user_id TEXT, created_at TEXT NOT NULL,
        CHECK((entry='single_entry') = (project_id IS NULL)),
        CHECK(visibility='shared' OR owner_user_id IS NOT NULL))""")
    connection.execute("CREATE INDEX IF NOT EXISTS agents_project ON agents(project_id)")
    connection.execute("""CREATE TABLE IF NOT EXISTS agent_sessions (
        agent_id TEXT NOT NULL, project_id TEXT NOT NULL, kind TEXT NOT NULL,
        provider TEXT NOT NULL, machine_id TEXT NOT NULL,
        native_session_id TEXT NOT NULL, bound_at TEXT NOT NULL,
        operation_id TEXT NOT NULL, PRIMARY KEY(agent_id, project_id, kind))""")
    for table, column, definition in (
        ("episodes", "agent_id", "TEXT"),
        ("agent_threads", "is_default", "INTEGER NOT NULL DEFAULT 1"),
    ):
        if column not in {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    connection.execute("DROP INDEX IF EXISTS agent_threads_space")
    connection.execute("DROP INDEX IF EXISTS agent_threads_project")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS agent_threads_default_space ON agent_threads(user_id) "
        "WHERE project_id IS NULL AND is_default=1"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS agent_threads_default_project ON agent_threads(user_id, project_id) "
        "WHERE project_id IS NOT NULL AND is_default=1"
    )
    backfill_agents(connection, session_task_kinds)


def backfill_agents(connection: sqlite3.Connection, session_task_kinds: Collection[str]) -> None:
    """Give every stored conversation, worker and episode its agent; existing rows stay.

    Each agent keeps the id its records already carry. An episode's agent follows
    the `ensure_turn_agent` rule. A session is the newest turn of the agent that
    holds a native session, as the turn scan read it.
    """
    connection.execute(
        "INSERT OR IGNORE INTO agents(agent_id,space_id,project_id,visibility,entry,"
        "owner_user_id,created_at) "
        f"SELECT thread_id, {_SPACE_ID}, project_id, 'private', "
        "CASE WHEN project_id IS NULL THEN 'single_entry' ELSE 'project' END, user_id, created_at "
        "FROM agent_threads"
    )
    connection.execute(
        "WITH first_turn(episode_id, agent_id) AS ("
        "SELECT episode_id, json_extract(request_json, '$.chat_id') FROM ("
        "SELECT episode_id, request_json, row_number() OVER (PARTITION BY episode_id "
        "ORDER BY created_at, rowid) AS position FROM graph_runs WHERE episode_id IS NOT NULL) "
        "WHERE position = 1), "
        "own(episode_id, continues_episode_id, agent_id) AS ("
        "SELECT episode.episode_id, episode.continues_episode_id, coalesce("
        "nullif(first_turn.agent_id, ''), nullif(state.chat_id, ''), "
        "CASE WHEN first_turn.episode_id IS NOT NULL THEN episode.episode_id END) "
        "FROM episodes AS episode LEFT JOIN first_turn USING (episode_id) "
        "LEFT JOIN experiment_episode_state AS state USING (episode_id)), "
        "chain(episode_id, agent_id) AS ("
        "SELECT episode_id, agent_id FROM own WHERE continues_episode_id IS NULL "
        "UNION ALL SELECT own.episode_id, coalesce(chain.agent_id, own.agent_id) FROM own "
        "JOIN chain ON own.continues_episode_id = chain.episode_id) "
        "UPDATE episodes SET agent_id = (SELECT chain.agent_id FROM chain "
        "WHERE chain.episode_id = episodes.episode_id) WHERE agent_id IS NULL"
    )
    connection.execute(
        "INSERT OR IGNORE INTO agents(agent_id,space_id,project_id,visibility,entry,"
        "owner_user_id,created_at) "
        f"SELECT agent_id, {_SPACE_ID}, project_id, 'shared', 'project', NULL, min(created_at) "
        "FROM ("
        "SELECT json_extract(request_json, '$.chat_id') AS agent_id, project_id, created_at "
        "FROM graph_runs "
        "UNION ALL SELECT worker_id, project_id, created_at FROM auto_research_child_work "
        "UNION ALL SELECT agent_id, project_id, created_at FROM episodes) "
        f"WHERE agent_id IS NOT NULL AND agent_id != '' AND project_id != {_SPACE_ID} "
        "GROUP BY agent_id"
    )
    kinds = sorted(session_task_kinds)
    connection.execute(
        "INSERT OR IGNORE INTO agent_sessions(agent_id,project_id,kind,provider,machine_id,"
        "native_session_id,bound_at,operation_id) "
        "SELECT agent_id, project_id, kind, provider, machine_id, native_session_id, created_at, "
        "operation_id FROM ("
        "SELECT json_extract(request_json, '$.chat_id') AS agent_id, project_id, kind, "
        "coalesce(json_extract(request_json, '$.provider'), '') AS provider, "
        "coalesce(json_extract(request_json, '$.run_on'), '') AS machine_id, "
        "native_session_id, created_at, operation_id, "
        "row_number() OVER (PARTITION BY json_extract(request_json, '$.chat_id'), project_id, "
        "kind ORDER BY created_at DESC, rowid DESC) AS newest "
        f"FROM graph_runs WHERE kind IN ({','.join('?' for _ in kinds)}) "
        "AND native_session_id IS NOT NULL AND native_session_id != '' "
        "AND (json_extract(request_json, '$.artifact_edit') IS NULL "
        "OR json_extract(request_json, '$.artifact_edit.origin_bound') = 0)) "
        "WHERE newest = 1 AND agent_id IN (SELECT agent_id FROM agents)",
        kinds,
    )


def ensure_agent(
    connection: sqlite3.Connection,
    agent_id: str,
    *,
    project_id: str | None,
    owner_user_id: str | None,
    created_at: str,
) -> None:
    """Record an agent once. Placement never changes after its first write."""
    connection.execute(
        "INSERT OR IGNORE INTO agents(agent_id,space_id,project_id,visibility,entry,"
        f"owner_user_id,created_at) VALUES(?, {_SPACE_ID}, ?, ?, ?, ?, ?)",
        (
            agent_id,
            project_id,
            "shared" if owner_user_id is None else "private",
            "single_entry" if project_id is None else "project",
            owner_user_id,
            created_at,
        ),
    )


def ensure_turn_agent(
    connection: sqlite3.Connection,
    *,
    agent_id: str | None,
    project_id: str,
    episode_id: str | None,
    created_at: str,
) -> None:
    """Name the agent a new turn belongs to, and the agent its episode runs on.

    An episode runs on the agent of the episode it continues, else on the agent
    of its first turn. A first turn that names no conversation is the episode's
    own agent.
    """
    if episode_id is not None:
        episode = connection.execute(
            "SELECT episode.agent_id, previous.agent_id AS continued FROM episodes AS episode "
            "LEFT JOIN episodes AS previous ON previous.episode_id = episode.continues_episode_id "
            "WHERE episode.episode_id = ?",
            (episode_id,),
        ).fetchone()
        if episode is not None and episode["agent_id"] is None:
            owner = episode["continued"] or agent_id or episode_id
            connection.execute(
                "UPDATE episodes SET agent_id = ? WHERE episode_id = ?", (owner, episode_id)
            )
            agent_id = agent_id or owner
    if agent_id is None:
        return
    connection.execute(
        "INSERT OR IGNORE INTO agents(agent_id,space_id,project_id,visibility,entry,"
        f"owner_user_id,created_at) SELECT ?, {_SPACE_ID}, ?, 'shared', 'project', NULL, ? "
        f"WHERE ? != {_SPACE_ID}",
        (agent_id, project_id, created_at, project_id),
    )


def agent(connection: sqlite3.Connection, agent_id: str) -> AgentRecord | None:
    row = connection.execute(
        "SELECT agent.*, display.title, display.archived_at FROM agents AS agent "
        "LEFT JOIN chat_display AS display ON display.chat_id = agent.agent_id "
        "AND display.project_id = coalesce(agent.project_id, agent.space_id) "
        "WHERE agent.agent_id = ?",
        (agent_id,),
    ).fetchone()
    return AgentRecord.model_validate(dict(row)) if row is not None else None


def agent_session(connection: sqlite3.Connection, agent_id: str) -> AgentSessionRecord | None:
    """The agent's most recently bound session."""
    row = connection.execute(
        "SELECT session.* FROM agent_sessions AS session LEFT JOIN graph_runs AS bound "
        "ON bound.operation_id = session.operation_id WHERE session.agent_id = ? "
        "ORDER BY bound.created_at DESC, bound.rowid DESC LIMIT 1",
        (agent_id,),
    ).fetchone()
    return AgentSessionRecord.model_validate(dict(row)) if row is not None else None


def bind_session(
    connection: sqlite3.Connection, session: AgentSessionRecord
) -> AgentSessionRecord | None:
    """Record this turn's session as the newest for its agent, project and task kind.

    Each kind keeps its own row, and a turn older than its row's never takes the
    session back. A chat's current session is read across the rows of the kinds
    that form one conversation, newest bound turn first, so node and project
    turns of one chat continue one session. Returns the session this one
    replaced in its row, when the native session changed; the caller owes that
    replacement a receipt.
    """
    key = (session.agent_id, session.project_id, session.kind)
    current = connection.execute(
        "SELECT session.*, bound.created_at AS turn_created_at, bound.rowid AS turn_rowid "
        "FROM agent_sessions AS session LEFT JOIN graph_runs AS bound "
        "ON bound.operation_id = session.operation_id "
        "WHERE session.agent_id = ? AND session.project_id = ? AND session.kind = ?",
        key,
    ).fetchone()
    turn = connection.execute(
        "SELECT created_at, rowid FROM graph_runs WHERE operation_id = ?", (session.operation_id,)
    ).fetchone()
    if turn is None:
        raise KeyError(session.operation_id)
    if (
        current is not None
        and current["turn_rowid"] is not None
        and (current["turn_created_at"], current["turn_rowid"]) > (turn[0], turn[1])
    ):
        return None
    connection.execute(
        "INSERT INTO agent_sessions(agent_id,project_id,kind,provider,machine_id,"
        "native_session_id,bound_at,operation_id) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(agent_id, project_id, kind) DO UPDATE SET "
        "provider=excluded.provider, machine_id=excluded.machine_id, "
        "native_session_id=excluded.native_session_id, "
        "bound_at=CASE WHEN agent_sessions.operation_id=excluded.operation_id "
        "THEN agent_sessions.bound_at ELSE excluded.bound_at END, "
        "operation_id=excluded.operation_id",
        (
            *key,
            session.provider,
            session.machine_id,
            session.native_session_id,
            session.bound_at,
            session.operation_id,
        ),
    )
    if current is None or current["native_session_id"] == session.native_session_id:
        return None
    data = dict(current)
    data.pop("turn_created_at")
    data.pop("turn_rowid")
    return AgentSessionRecord.model_validate(data)


def move_project_agents(connection: sqlite3.Connection, old_id: str, project_id: str) -> None:
    for table in ("agents", "agent_sessions"):
        connection.execute(
            f"UPDATE {table} SET project_id = ? WHERE project_id = ?", (project_id, old_id)
        )


def rekey_project_sessions(
    connection: sqlite3.Connection, project_id: str, mapped: Callable[[str], str]
) -> None:
    """Name each bound session's machine as its turn's rewritten request now does."""
    rows = connection.execute(
        "SELECT rowid, machine_id FROM agent_sessions WHERE project_id = ?", (project_id,)
    ).fetchall()
    for rowid, machine_id in rows:
        connection.execute(
            "UPDATE agent_sessions SET machine_id = ? WHERE rowid = ?", (mapped(machine_id), rowid)
        )


def delete_project_agents(connection: sqlite3.Connection, project_id: str) -> dict[str, int]:
    """Delete a project's sessions, then its agents, so no session outlives its agent."""
    sessions = connection.execute(
        "DELETE FROM agent_sessions WHERE project_id = ? OR agent_id IN "
        "(SELECT agent_id FROM agents WHERE project_id = ?)",
        (project_id, project_id),
    ).rowcount
    agents = connection.execute("DELETE FROM agents WHERE project_id = ?", (project_id,)).rowcount
    return {"agent_sessions": sessions, "agents": agents}


class AgentStoreMixin(StoreMixinBase):
    def agent(self, agent_id: str) -> AgentRecord | None:
        with self.connection() as connection:
            return agent(connection, agent_id)

    def agent_session(self, agent_id: str) -> AgentSessionRecord | None:
        with self.connection() as connection:
            return agent_session(connection, agent_id)
