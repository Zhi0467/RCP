"""Pull-only, commit-ordered research digest; canonical history is read only."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import UTC, datetime
from urllib.parse import quote, urlencode

from rcp.core.attention import project_graph_attention
from rcp.core.models import GraphBranchMetadata, GraphState, Patch
from rcp.core.transition_models import GraphTargetRef
from rcp.episode_health import episode_needs_action, load_episode_health
from rcp.history.delta import SemanticDelta, semantic_delta
from rcp.history.engine import ProjectHistoryEngine
from rcp.history.queries import HeadSpec
from rcp.limits import DIGEST_RECHECK_SECONDS, DIGEST_STOP_TIMEOUT_SECONDS
from rcp.server_ops.maintenance import MaintenanceAdmissionClosed
from rcp.storage.chat_names import chat_titles
from rcp.storage.digest import append_digest_event
from rcp.transport import StateUnavailable

_LOG = logging.getLogger(__name__)
# Reports are the only operational lines; ended and failed runs stay stored but unshown.
_REPORTS = {"consolidation_report", "episode_report"}


def deep_link(project_id: str, target: str, kind: str, item_id: str) -> str:
    return (
        f"#/projects/{quote(project_id, safe='')}/targets/{quote(target, safe='')}"
        f"/{kind}/{quote(item_id, safe='')}"
    )


def _attention(state: GraphState) -> dict[tuple[str, str], str]:
    attention = project_graph_attention(state)
    return {
        **{
            ("proposal", item): state.proposals[item].title
            for item in attention.pending_proposal_ids
        },
        **{
            ("decision", item): state.nodes[item].title
            for item in attention.decisions_awaiting_choice_ids
        },
    }


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _viewers_own(event: dict, user_id: str, chat_reads: dict | None) -> bool:
    """Whether the viewer already knows this change, so their digest leaves it out.

    A member's direct edit is theirs. A Work turn they asked for is theirs once
    they have seen it finish: their chat read marker reaches the change. One
    that finished after they left stays news until they read the chat. This is
    a visibility rule only; the provider agent stays the turn's author.
    """
    if event["actor_user_id"] != user_id:
        return False
    source_kind = event["payload"].get("source_kind")
    if source_kind == "member":
        return True
    if source_kind != "chat" or chat_reads is None:
        return False
    chat_id = event["source_key"].removeprefix("chat:")
    read_through = chat_reads["reads"].get(chat_id, chat_reads["baseline"])
    return read_through is not None and _instant(read_through) >= _instant(event["created_at"])


def attribution(store, project_id: str, patch: Patch) -> dict:
    """Resolve source once, with explicit provenance ahead of producer fallbacks."""
    result: dict[str, str | None] = {
        "actor_user_id": None,
        "report_artifact_id": None,
        "deep_link": None,
    }
    if patch.branch_merge is not None and patch.branch_merge.episode_id is not None:
        episode_id = patch.branch_merge.episode_id
        kind, key, label = "episode", episode_id, _episode_title(store, episode_id)
        result["deep_link"] = deep_link(project_id, "main", "episode", episode_id)
    elif patch.branch_merge is not None:
        # A human-owned fork has no episode; its merge belongs to the dispatching member.
        if patch.authorized_by is None:
            kind, key, label = "unattributed", "human", "Unattributed"
        else:
            kind, key, label = (
                "member",
                patch.authorized_by.user_id,
                patch.authorized_by.display_name,
            )
            result["actor_user_id"] = key
    else:
        operation_id = patch.source_operation_id or patch.task_id
        consolidation = (
            store.consolidation_run_for_operation(operation_id) if operation_id else None
        )
        if consolidation is not None:
            kind, key, label = "consolidation", consolidation.run_id, "Nightly consolidation"
            result["report_artifact_id"] = consolidation.report_artifact_id
            result["deep_link"] = deep_link(
                project_id, consolidation.graph_target.key, "consolidation", key
            )
        elif patch.producer == "human":
            if patch.authorized_by is None:
                kind, key, label = "unattributed", "human", "Unattributed"
            else:
                kind, key, label = (
                    "member",
                    patch.authorized_by.user_id,
                    patch.authorized_by.display_name,
                )
                result["actor_user_id"] = key
        elif patch.kind in {"seed", "refresh"}:
            kind, key, label = "ingestion", "ingestion", "Ingestion"
        else:
            task = store.agent_task(operation_id) if operation_id else None
            chat_id = task.request.get("chat_id") if task else None
            if (
                task
                and task.kind in {"node_chat", "project_chat"}
                and task.request.get("mode") == "work"
                and chat_id
            ):
                kind, key = "chat", str(chat_id)
                with store.connection() as conn:
                    row = conn.execute(
                        "SELECT title FROM chat_display WHERE project_id=? AND chat_id=?",
                        (project_id, key),
                    ).fetchone()
                label = row[0] if row and row[0] else "Chat"
                result["deep_link"] = (
                    f"#/projects/{quote(project_id, safe='')}?view=chats&chat={quote(key, safe='')}"
                )
                # The member who asked for this Work turn; see `_viewers_own`.
                if task.authorized_by is not None:
                    result["actor_user_id"] = task.authorized_by.user_id
            elif patch.producer == "system":
                kind, key, label = "system", "system", "System"
            else:
                kind, key, label = "agent", "agent", "Agent"
    return dict(result, source_kind=kind, source_key=f"{kind}:{key}", source_label=label)


def _episode_title(store, episode_id: str) -> str:
    episode = store.episode(episode_id)
    if episode and episode.root_operation_id:
        task = store.agent_task(episode.root_operation_id)
        if task:
            return str(task.request.get("title") or task.request.get("objective") or "Episode")
    return "Episode"


def graph_event(
    store,
    project_id: str,
    target: str,
    before: GraphState,
    patch: Patch,
    after: GraphState,
    *,
    delta: SemanticDelta | None = None,
) -> dict:
    if delta is None:
        delta = semantic_delta(before, after)
    nodes = {item.node_id for item in delta.nodes}
    for item in delta.edges:
        for edge in (item.before, item.after):
            if edge:
                nodes.update((edge.source, edge.target))
    for item in delta.proposals:
        for proposal in (item.before, item.after):
            if proposal:
                nodes.update(proposal.related_node_ids)
    edits = sum(
        len(getattr(delta, field))
        for field in ("nodes", "edges", "proposals", "ambiguities", "glossary", "globals")
    )
    old, new = _attention(before), _attention(after)
    attention = [
        {
            "kind": kind,
            "item_id": item,
            "title": new.get((kind, item), old.get((kind, item))),
            "active": (kind, item) in new,
        }
        for kind, item in sorted(old.keys() ^ new.keys())
    ]
    source = attribution(store, project_id, patch)
    payload = {
        "source_kind": source.pop("source_kind"),
        "edits": edits,
        "non_node_edits": edits - len(delta.nodes),
        "edited_node_ids": [item.node_id for item in delta.nodes],
        "report_artifact_id": source.pop("report_artifact_id"),
        "deep_link": source.pop("deep_link"),
        "attention": attention,
    }
    if patch.branch_merge is not None:
        payload["merged_branch_revision"] = patch.branch_merge.branch_head.revision
        payload["merged_branch_id"] = patch.branch_merge.branch_id
    kind = "graph_change"
    if target != "main":
        kind = "branch_change"
        branch_id = target.removeprefix("branch:")
        branch = store.graph_branch(project_id, branch_id)
        episode_id = branch.owner_episode_id if branch else None
        payload.update(
            branch_id=branch_id,
            episode_id=episode_id,
            title=_episode_title(store, episode_id)
            if episode_id
            else (branch.name if branch else "Branch"),
            deep_link=deep_link(project_id, target, "episode", episode_id)
            if episode_id
            else _app_hash(project_id, "graph", branch_id=branch_id),
            needs_action=any(kind == "decision" for kind, _ in new),
        )
    return dict(
        project_id=project_id,
        kind=kind,
        item_id=str(patch.revision),
        target=target,
        node_ids=sorted(nodes),
        payload=payload,
        created_at=patch.created_at.isoformat(),
        **source,
    )


def _app_hash(project_id: str, view: str, **params: str) -> str:
    """An in-app route in `projectViewHash` form, so a digest link needs no resolving."""
    query = urlencode({"view": view, **params}, quote_via=quote)
    return f"#/projects/{quote(project_id, safe='')}?{query}"


def _run_link(project_id: str, branch_id: str, episode_id: str) -> str:
    """An Auto-research run opens its orchestrator card on its own branch."""
    return _app_hash(project_id, "chats", chat=episode_id, branch_id=branch_id)


def _experiment_link(project_id: str, episode) -> str:
    """The Runs entry of one Experiment episode, as `experimentBoardHref` writes it."""
    target = episode.graph_target
    route = (
        f"&target=branch&branch={quote(target.branch_id, safe='')}"
        if target.kind == "branch"
        else "&target=main"
    )
    node, episode_id = (
        quote(value, safe="") for value in (episode.control_node_id, episode.episode_id)
    )
    return f"{_app_hash(project_id, 'runs')}&experiment={node}&episode={episode_id}{route}"


def _run_child(event_target: str, episode_id: str, runs: dict[str, str], chain) -> bool:
    """An episode on a run's branch that is no orchestrator: neither the chain
    root nor a continuation, which keeps the root's branch under a new id."""
    return event_target in runs and episode_id not in chain


def assemble_digest(
    project_id: str,
    user_id: str,
    events: list[dict],
    mark: dict | None,
    cursor: int,
    state: GraphState | None = None,
    chat_reads: dict | None = None,
    *,
    target: str = "main",
    runs: dict[str, str] | None = None,
    chain: frozenset[str] = frozenset(),
    archived: frozenset[str] = frozenset(),
) -> dict:
    """The sole rendered-line grouping function, also used by landing counts.

    `target` is the graph the reader has open. `runs` names each Auto-research
    run by its branch target key, and `chain` holds every orchestrator
    episode id, roots and continuations. Only main's own edits are main's changes; a
    run reaches main's digest solely as one Needs you line, while something on
    its branch waits for the human: an open question, Proposal, or Decision
    there, or its own orchestrator episode. Its
    child episodes' health is never a line, here or on its branch.
    """
    runs = runs or {}
    resets = {}
    for event in events:
        if event["kind"] == "reset":
            resets[event["target"]] = max(resets.get(event["target"], 0), event["seq"])
    # A reset replaces graph history only; question and episode attention live in
    # SQLite and stay current across it.
    events = [
        event
        for event in events
        if event["kind"] not in {"graph_change", "branch_change"}
        or event["seq"] > resets.get(event["target"], 0)
    ]
    attention, latest_nodes, groups, reports = {}, {}, {}, {}
    node_edits = {}
    merged = {}
    for event in events:
        if "merged_branch_revision" in event["payload"]:
            branch_id = event["payload"].get("merged_branch_id") or event[
                "source_key"
            ].removeprefix("episode:")
            merged[branch_id] = max(
                merged.get(branch_id, 0), event["payload"]["merged_branch_revision"]
            )
    for event in events:
        payload = event["payload"]
        kind, item_id, event_target = event["kind"], event["item_id"], event["target"]
        if kind in {"graph_change", "branch_change"}:
            if kind == "branch_change" and int(item_id) <= merged.get(
                payload.get("branch_id", payload.get("episode_id")), -1
            ):
                continue
            in_scope = event_target == target
            if not in_scope and not (target == "main" and event_target in runs):
                continue
            for entry in payload.get("attention", []):
                identity = (event_target, entry["kind"], entry["item_id"])
                attention[identity] = dict(
                    entry,
                    target=event_target,
                    created_at=event["created_at"],
                    deep_link=deep_link(project_id, event_target, entry["kind"], entry["item_id"]),
                )
            if not in_scope or _viewers_own(event, user_id, chat_reads) or not payload["edits"]:
                continue
            key = event["source_key"]
            group = groups.setdefault(
                key,
                dict(
                    source_key=key,
                    source_kind=payload["source_kind"],
                    label=event["source_label"],
                    edits=0,
                    node_ids=[],
                    deep_link=payload.get("deep_link"),
                ),
            )
            group["edits"] += payload.get(
                "non_node_edits", max(0, payload["edits"] - len(event["node_ids"]))
            )
            for node_id in payload.get("edited_node_ids", event["node_ids"]):
                node_edits[(key, node_id)] = node_edits.get((key, node_id), 0) + 1
            for node_id in event["node_ids"]:
                latest_nodes[node_id] = key
        elif kind in {"question_attention", "episode_attention"}:
            if event_target != target and not (target == "main" and event_target in runs):
                continue
            item_kind = "question" if kind == "question_attention" else "episode"
            if item_kind == "episode" and _run_child(event_target, item_id, runs, chain):
                continue
            attention[(event_target, item_kind, item_id)] = dict(
                kind=item_kind,
                item_id=item_id,
                target=event_target,
                title=payload["title"],
                active=payload["active"],
                created_at=event["created_at"],
                deep_link=payload.get("deep_link"),
            )
        elif kind in _REPORTS and event_target == target:
            reports[(kind, item_id)] = dict(
                kind=kind,
                item_id=item_id,
                title=payload["title"],
                report_artifact_id=payload.get("report_artifact_id"),
                deep_link=payload.get("deep_link"),
                created_at=event["created_at"],
            )
    for node_id, key in sorted(latest_nodes.items()):
        groups[key]["node_ids"].append(node_id)
        groups[key]["edits"] += node_edits.get((key, node_id), 0)
    needs, run_needs = [], {}
    for item in attention.values():
        # An archived branch is silenced: nothing on it needs the human.
        if not item.pop("active") or item["target"] in archived:
            continue
        run = item["target"] in runs and (item["kind"] == "episode" or target == "main")
        if run:
            run_needs[item["target"]] = max(run_needs.get(item["target"], ""), item["created_at"])
        else:
            needs.append(item)
    for run_target, created_at in run_needs.items():
        branch_id = run_target.removeprefix("branch:")
        needs.append(
            dict(
                kind="auto_research",
                item_id=branch_id,
                title=runs[run_target],
                target=run_target,
                deep_link=_app_hash(project_id, "chats", branch_id=branch_id),
                created_at=created_at,
            )
        )
    changed_ids = sorted(
        node_id
        for node_id in latest_nodes
        if target == "main" and (state is None or node_id in state.nodes)
    )
    changed = [group for group in groups.values() if group["edits"] or group["node_ids"]]
    report_lines = list(reports.values())
    return dict(
        cursor=cursor,
        mark=mark,
        target=target,
        needs_you=needs,
        changed=changed,
        reports=report_lines,
        changed_node_ids=changed_ids,
        count=len(needs) + len(changed) + len(report_lines),
    )


def _main_state(catalog, project_id: str, *, required: bool) -> GraphState | None:
    service = catalog.loaded_service(project_id)
    if service is None:
        workspace = catalog.workspace_for_display(project_id)
        if workspace is not None and workspace.remote:
            service = catalog.open_display(project_id)
    if service is not None:
        with service.history.workspace.read_snapshot() as root:
            return GraphState.model_validate_json((root / "graph.json").read_text())
    snapshot = catalog.cached_snapshot(project_id)
    if snapshot is None:
        if not required:
            return None
        raise StateUnavailable("The project graph snapshot is not available yet.")
    return GraphState.model_validate(snapshot["graph"])


def _branch_state(catalog, project_id: str, branch_id: str) -> GraphState | None:
    """The branch's own graph, which names Experiments it created or renamed."""
    service = catalog.loaded_service(project_id)
    if service is None:
        workspace = catalog.workspace_for_display(project_id)
        if workspace is not None and workspace.remote:
            service = catalog.open_display(project_id)
    if service is None:
        return None
    target = GraphTargetRef(kind="branch", branch_id=branch_id)
    engine = service.history.history_engine
    return engine.state_at(engine.resolve(HeadSpec(revision="head"), default_target=target))


def read_digest(
    store, catalog, project_id: str, user_id: str, branch_id: str | None = None
) -> dict:
    mark, cursor, events = store.digest_snapshot(project_id, user_id)
    kinds = {event["kind"] for event in events}
    if branch_id is not None:
        # Changed-node dots are main's only, so a branch graph just names Experiments.
        state = (
            _branch_state(catalog, project_id, branch_id) if "episode_attention" in kinds else None
        )
    else:
        # Main filters the dots and names Experiments; only the dots require it.
        state = (
            _main_state(catalog, project_id, required="graph_change" in kinds)
            if kinds & {"graph_change", "episode_attention"}
            else None
        )
    target = "main" if branch_id is None else "branch:" + branch_id
    return _assemble_current(store, project_id, user_id, events, mark, cursor, state, target)


def _orchestrator_episodes(conn, project_id: str) -> frozenset[str]:
    """Every Auto-research episode: chain roots and their continuations."""
    return frozenset(
        str(row[0])
        for row in conn.execute(
            "SELECT episode_id FROM episodes WHERE project_id=? AND mode='auto_research'",
            (project_id,),
        )
    )


def _archived_branches(conn, project_id: str) -> frozenset[str]:
    """Branch target keys the human archived."""
    return frozenset(
        "branch:" + str(row[0])
        for row in conn.execute(
            "SELECT branch_id FROM graph_branches WHERE project_id=? AND archived=1",
            (project_id,),
        )
    )


def _auto_research_runs(conn, project_id: str) -> dict[str, str]:
    """Each Auto-research run's branch target key and its branch name."""
    return {
        "branch:" + row[0]: row[1]
        for row in conn.execute(
            "SELECT b.branch_id, b.name FROM graph_branches AS b JOIN episodes AS e "
            "ON e.project_id=b.project_id AND e.episode_id=b.owner_episode_id "
            "WHERE b.project_id=? AND e.mode='auto_research' "
            "AND json_extract(e.graph_target_json, '$.branch_id')=b.branch_id",
            (project_id,),
        )
    }


def _drop_closed_attention(store, project_id: str, events: list[dict]) -> list[dict]:
    """Drop every opening of a question or episode that no longer needs the human.

    Removing all of an item's openings is the same as hiding its latest state,
    and it lets a run fold only the children that still need the human.
    """
    opened = {
        kind: [e["item_id"] for e in events if e["kind"] == kind and e["payload"]["active"]]
        for kind in ("question_attention", "episode_attention")
    }
    open_ids = set()
    if opened["question_attention"]:
        with store.connection() as conn:
            open_ids |= {
                row[0]
                for row in conn.execute(
                    "SELECT question_id FROM questions WHERE state='pending' AND withdrawn_readonly=0 "
                    "AND question_id IN (SELECT value FROM json_each(?))",
                    (json.dumps(opened["question_attention"]),),
                )
            }
    if opened["episode_attention"]:
        _, active = _episode_attention(store, project_id, opened["episode_attention"])
        open_ids |= {episode_id for episode_id, needs in active.items() if needs}
    return [
        event
        for event in events
        if event["kind"] not in opened
        or not event["payload"]["active"]
        or event["item_id"] in open_ids
    ]


def _drop_deleted_reports(store, events: list[dict]) -> list[dict]:
    """Both report kinds leave the digest when their artifact is gone."""
    reports = [e for e in events if e["kind"] in _REPORTS]
    if not reports:
        return events
    with store.connection() as conn:
        live = {
            row[0]
            for row in conn.execute(
                """
                SELECT json_extract(event.value, '$.seq')
                FROM json_each(?) AS event
                LEFT JOIN episode_reports AS report
                  ON json_extract(event.value, '$.kind') = 'episode_report'
                 AND report.report_id = json_extract(event.value, '$.item_id')
                JOIN artifacts AS artifact ON artifact.artifact_id = CASE
                    WHEN json_extract(event.value, '$.kind') = 'episode_report'
                    THEN report.artifact_id
                    ELSE json_extract(event.value, '$.payload.report_artifact_id') END
                """,
                (json.dumps(reports),),
            )
        }
    return [e for e in events if e["kind"] not in _REPORTS or e["seq"] in live]


def _assemble_current(store, project_id, user_id, events, mark, cursor, state=None, target="main"):
    with store.connection() as conn:
        runs = _auto_research_runs(conn, project_id)
        chain = _orchestrator_episodes(conn, project_id)
        archived = _archived_branches(conn, project_id)
    result = assemble_digest(
        project_id,
        user_id,
        _drop_deleted_reports(store, _drop_closed_attention(store, project_id, events)),
        mark,
        cursor,
        state,
        store.chat_read_markers(project_id, user_id),
        target=target,
        runs=runs,
        chain=chain,
        archived=archived,
    )
    _present_current(store, project_id, result, state, runs)
    return result


def _present_current(store, project_id, result, state, runs) -> None:
    """Live titles and final in-app links; stored payload titles predate both."""
    needs, reports = result["needs_you"], result["reports"]
    question_ids = [item["item_id"] for item in needs if item["kind"] == "question"]
    report_ids = [item["item_id"] for item in reports if item["kind"] == "episode_report"]
    with store.connection() as conn:
        chat_groups = [
            group for group in result["changed"] if group["source_key"].startswith("chat:")
        ]
        titles = chat_titles(
            conn, project_id, [group["source_key"].removeprefix("chat:") for group in chat_groups]
        )
        for group in chat_groups:
            group["label"] = titles.get(group["source_key"].removeprefix("chat:"), group["label"])
        owners = {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                "SELECT question_id, owner_kind, owner_id FROM questions "
                "WHERE question_id IN (SELECT value FROM json_each(?))",
                (json.dumps(question_ids),),
            )
        }
        report_rows = {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                "SELECT report_id, artifact_id, display_title FROM episode_reports "
                "WHERE report_id IN (SELECT value FROM json_each(?))",
                (json.dumps(report_ids),),
            )
        }
    episode_ids = {item["item_id"] for item in needs if item["kind"] == "episode"} | {
        owner_id for kind, owner_id in owners.values() if kind == "episode"
    }
    episodes = store.episodes_by_ids(sorted(episode_ids)) if episode_ids else {}

    def run_link(branch_id: str) -> str:
        owner = store.branch_owner(project_id, branch_id)
        assert owner is not None  # Only episode-owned root branches are in runs.
        return _run_link(project_id, branch_id, owner)

    def episode_link(episode) -> str | None:
        if episode.graph_target.key in runs:
            return run_link(episode.graph_target.branch_id)
        if episode.mode == "experiment_loop" and episode.control_node_id:
            return _experiment_link(project_id, episode)
        return None

    for item in needs:
        if item["kind"] == "auto_research":
            item["deep_link"] = run_link(item["target"].removeprefix("branch:"))
        elif item["kind"] == "episode" and (episode := episodes.get(item["item_id"])):
            node = state.nodes.get(episode.control_node_id or "") if state else None
            item["title"] = node.title if node else (episode.control_node_id or item["title"])
            item["deep_link"] = episode_link(episode) or item["deep_link"]
        elif item["kind"] == "question" and item["item_id"] in owners:
            owner_kind, owner_id = owners[item["item_id"]]
            if owner_kind == "chat":
                branch = {}
                if item["target"] != "main":
                    branch["branch_id"] = item["target"].removeprefix("branch:")
                item["deep_link"] = _app_hash(project_id, "chats", chat=owner_id, **branch)
            elif episode := episodes.get(owner_id):
                item["deep_link"] = episode_link(episode) or item["deep_link"]
    for item in reports:
        if item["item_id"] in report_rows:
            artifact_id, title = report_rows[item["item_id"]]
            item["report_artifact_id"] = artifact_id
            item["title"] = title or item["title"]
    for group in result["changed"]:
        if group["source_kind"] == "episode":
            # A merged branch is named by its branch, like its run line was.
            episode = store.episode(group["source_key"].removeprefix("episode:"))
            if episode and episode.graph_target.key in runs:
                group["label"] = runs[episode.graph_target.key]
                group["deep_link"] = run_link(episode.graph_target.branch_id)
    if result["target"] in runs:
        for group in result["changed"]:
            group["deep_link"] = run_link(result["target"].removeprefix("branch:"))


def digest_counts(store, project_ids: list[str], user_id: str) -> dict[str, int]:
    batches = store.digest_event_batches(project_ids, user_id)
    counts = dict.fromkeys(project_ids, 0)
    counts.update(
        {
            project_id: _assemble_current(store, project_id, user_id, events, mark, cursor)["count"]
            for project_id, (mark, cursor, events) in batches.items()
        }
    )
    return counts


def _merged_revision(conn, project_id, target):
    row = conn.execute(
        "SELECT payload_json FROM digest_events WHERE project_id=? AND kind='graph_change' "
        "AND coalesce(json_extract(payload_json, '$.merged_branch_id'), substr(source_key, 9))=? "
        "AND json_extract(payload_json, '$.merged_branch_revision') IS NOT NULL "
        "ORDER BY seq DESC LIMIT 1",
        (project_id, target.removeprefix("branch:")),
    ).fetchone()
    return json.loads(row[0])["merged_branch_revision"] if row else -1


class DigestProjector:
    def __init__(self, store, catalog, *, admission, startup_effect_fence=None):
        self.store, self.catalog = store, catalog
        self.admission, self.startup_effect_fence = admission, startup_effect_fence
        self._stop, self._wake = threading.Event(), threading.Event()
        self._lock = threading.RLock()
        self._dirty_lock = threading.Lock()
        self._dirty: set[tuple[str, str]] | None = None
        self._thread = None

    def start(self):
        if self.is_running():
            return
        self._stop.clear()
        self._dirty = None
        self._wake.set()
        self._thread = threading.Thread(target=self._run, name="rcp-digest", daemon=True)
        self._thread.start()

    def stop(self, timeout=DIGEST_STOP_TIMEOUT_SECONDS):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def signal(self, project_id, revision=None):
        self.signal_branch(project_id, "main", revision)

    def signal_branch(self, project_id, target, revision):
        self._mark_dirty(project_id, target)
        self._wake.set()

    def _mark_dirty(self, project_id, target):
        with self._dirty_lock:
            if self._dirty is not None:
                self._dirty.add((project_id, target))

    def _run(self):
        while not self._stop.is_set():
            self._wake.wait(DIGEST_RECHECK_SECONDS)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.run_pass()
            except MaintenanceAdmissionClosed:
                return
            except Exception:
                _LOG.exception("Digest projection failed")

    def run_pass(self):
        if self.startup_effect_fence is not None:
            self.startup_effect_fence.require_open("digest projection")
        with self.admission.mutation("digest projection"), self._lock:
            with self._dirty_lock:
                dirty, self._dirty = self._dirty, set()
            for project in self.store.projects():
                if project.retired_at is not None:
                    continue
                project_id = project.project_id
                try:
                    targets = (
                        None
                        if dirty is None
                        else {target for pid, target in dirty if pid == project_id}
                    )
                    if targets is None or targets:
                        self.reconcile_project(project_id, targets)
                    else:
                        lagging = self._lagging(project_id)
                        if lagging:
                            self.reconcile_project(project_id, lagging)
                    self._observe_episodes(project_id)
                except Exception:
                    # Retry on the next recheck or change signal, never at once: a
                    # project that keeps failing would otherwise spin this thread.
                    self._mark_dirty(project_id, "main")
                    _LOG.exception("Digest reconciliation failed for %s", project_id)

    def _branch_targets(self, project_id):
        live = {episode.graph_target.key for episode in self.store.live_episodes(project_id)}
        with self.store.connection() as conn:
            return {
                "branch:" + branch.branch_id: branch.branch_id
                for branch in self.store.graph_branches(project_id)
                if "branch:" + branch.branch_id in live
                or _merged_revision(conn, project_id, "branch:" + branch.branch_id) < 0
            }

    def _lagging(self, project_id):
        service = self.catalog.loaded_service(project_id)
        if service is None:
            return set()
        with self.store.connection() as conn:
            heads = {
                row["target"]: row["revision"]
                for row in conn.execute(
                    "SELECT target,revision FROM digest_heads WHERE project_id=?", (project_id,)
                )
            }
        targets = set()
        if service.history.current_accepted_revision() != heads.get("main"):
            targets.add("main")
        for target, branch_id in self._branch_targets(project_id).items():
            try:
                path = service.history.root / "branches" / branch_id / "branch.json"
                metadata = GraphBranchMetadata.model_validate_json(path.read_text())
                revision = (
                    metadata.head.revision
                    if metadata.head.revision > metadata.effective_parent_head.revision
                    else 0
                )
                if revision != heads.get(target):
                    targets.add(target)
            except FileNotFoundError:
                continue
            except Exception:
                _LOG.exception("Digest branch check failed for %s/%s", project_id, target)
        return targets

    def reconcile_project(self, project_id, targets=None):
        with self._lock:
            service = self.catalog.open(project_id)
            history = service.history
            engine = service.history.history_engine
            with self.store.connection() as conn:
                initial = (
                    conn.execute(
                        "SELECT 1 FROM digest_heads WHERE project_id=? AND target='main'",
                        (project_id,),
                    ).fetchone()
                    is None
                )
            # Member marks wait for the main checkpoint, so on a project's first
            # pass the main checkpoint comes last: existing branches are
            # baselined before any member can start counting.
            if not initial and (targets is None or "main" in targets):
                self._project_history(project_id, "main", engine)
            branches = (
                self._branch_targets(project_id)
                if targets is None or initial
                else {
                    target: target.removeprefix("branch:") for target in targets if target != "main"
                }
            )
            for target, branch_id in branches.items():
                try:
                    if not (history.root / "branches" / branch_id / "branch.json").exists():
                        continue
                    self._project_history(project_id, target, engine)
                except Exception:
                    _LOG.exception("Digest branch projection failed for %s/%s", project_id, target)
            if initial and (targets is None or "main" in targets):
                # Episodes too: a mark made before their baseline would hide
                # an episode that starts needing the human in that gap.
                self._observe_episodes(project_id)
                self._project_history(project_id, "main", engine)

    def _project_history(self, project_id, target, engine: ProjectHistoryEngine):
        with self.store.connection() as conn:
            head = conn.execute(
                "SELECT revision,patch_id FROM digest_heads WHERE project_id=? AND target=?",
                (project_id, target),
            ).fetchone()
        graph_target = (
            GraphTargetRef()
            if target == "main"
            else GraphTargetRef(kind="branch", branch_id=target.removeprefix("branch:"))
        )
        generation = engine.snapshot(graph_target)
        resolved = generation.head(generation.result.state.revision)
        if generation.result.state.replay_status != "complete":
            raise ValueError("digest requires a complete accepted history")
        identities = {}
        prefix = hashlib.sha256()
        accepted = [step for step in generation.index if step.accepted]
        for step in accepted:
            patch = step.patch
            prefix.update(patch.model_dump_json().encode())
            identities[patch.revision] = prefix.hexdigest()
        current_revision = max(identities, default=0)
        # A newly discovered branch belongs to an already observed project. Its
        # first commit must survive a crash even if branch creation had no signal.
        discovered_branch = False
        merged_revision = -1
        if target != "main":
            with self.store.connection() as conn:
                discovered_branch = (
                    conn.execute(
                        "SELECT 1 FROM digest_heads WHERE project_id=? AND target='main'",
                        (project_id,),
                    ).fetchone()
                    is not None
                )
                merged_revision = _merged_revision(conn, project_id, target)
        reset = head is not None and (
            head["revision"] != 0 and identities.get(head["revision"]) != head["patch_id"]
        )
        events = (
            []
            if reset or (head is None and not discovered_branch)
            else [
                graph_event(
                    self.store,
                    project_id,
                    target,
                    step.before_state,
                    step.patch,
                    step.after_state,
                    delta=step.delta,
                )
                for step in engine._steps(
                    generation,
                    resolved,
                    from_revision=max(merged_revision, head["revision"] if head else 0) + 1,
                )
                if step.accepted
                and step.patch.revision > merged_revision
                and (
                    (head is not None and step.patch.revision > head["revision"])
                    or (head is None and discovered_branch)
                )
            ]
        )
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if (
                conn.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone()
                is None
            ):
                return
            current_head = conn.execute(
                "SELECT revision,patch_id FROM digest_heads WHERE project_id=? AND target=?",
                (project_id, target),
            ).fetchone()
            if current_head != head:
                return
            if reset:
                append_digest_event(
                    conn,
                    project_id=project_id,
                    target=target,
                    kind="reset",
                    item_id=str(current_revision),
                    created_at=self.store.now(),
                )
            for event in events:
                append_digest_event(conn, **event)
            conn.execute(
                "INSERT INTO digest_heads(project_id,target,revision,patch_id) VALUES(?,?,?,?) ON CONFLICT(project_id,target) DO UPDATE SET revision=excluded.revision,patch_id=excluded.patch_id",
                (project_id, target, current_revision, identities.get(current_revision, "")),
            )
        if reset:
            _LOG.warning(
                "Digest history %s/%s no longer contains its head; rebaselined", project_id, target
            )

    def _observe_episodes(self, project_id):
        episodes, active = _episode_attention(self.store, project_id)
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if (
                conn.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone()
                is None
            ):
                return
            initial = (
                conn.execute(
                    "SELECT 1 FROM digest_heads WHERE project_id=? AND target='episodes'",
                    (project_id,),
                ).fetchone()
                is None
            )
            conn.execute(
                "INSERT OR IGNORE INTO digest_heads(project_id,target,revision,patch_id) VALUES(?, 'episodes', 0, '')",
                (project_id,),
            )
            for episode in episodes:
                key = "episode:" + episode.episode_id
                previous = conn.execute(
                    "SELECT revision FROM digest_heads WHERE project_id=? AND target=?",
                    (project_id, key),
                ).fetchone()
                current = int(active[episode.episode_id])
                if previous is not None and previous[0] == current:
                    continue
                conn.execute(
                    "INSERT INTO digest_heads(project_id,target,revision,patch_id) VALUES(?,?,?, '') "
                    "ON CONFLICT(project_id,target) DO UPDATE SET revision=excluded.revision",
                    (project_id, key, current),
                )
                # An existing episode is silently baselined on first observation.
                # A new branch attention event is already an observed graph change.
                branch = conn.execute(
                    "SELECT 1 FROM digest_events WHERE project_id=? AND kind='branch_change' AND target=? LIMIT 1",
                    (project_id, episode.graph_target.key),
                ).fetchone()
                if previous is None and (not current or (initial and not branch)):
                    continue
                append_digest_event(
                    conn,
                    project_id=project_id,
                    kind="episode_attention",
                    item_id=episode.episode_id,
                    target=episode.graph_target.key,
                    created_at=self.store.now(),
                    payload=dict(
                        active=bool(current),
                        title=_episode_title(self.store, episode.episode_id),
                        deep_link=deep_link(
                            project_id, episode.graph_target.key, "episode", episode.episode_id
                        ),
                    ),
                )


def _episode_attention(store, project_id, episode_ids=None):
    episodes = (
        store.episodes(project_id, limit=None)
        if episode_ids is None
        else list(store.episodes_by_ids(episode_ids).values())
    )
    health = load_episode_health(store, episodes)
    active = {}
    with store.connection() as conn:
        runs = _auto_research_runs(conn, project_id)
        for episode in episodes:
            current, _, _, blocked = health[episode.episode_id]
            active[episode.episode_id] = episode_needs_action(current, blocked)
            # An Auto-research branch carries its own Decisions into the digest;
            # another episode's branch Decision surfaces through its episode.
            if episode.graph_target.kind != "branch" or episode.graph_target.key in runs:
                continue
            branch = conn.execute(
                "SELECT item_id,payload_json FROM digest_events WHERE project_id=? AND kind='branch_change' AND target=? ORDER BY seq DESC LIMIT 1",
                (project_id, episode.graph_target.key),
            ).fetchone()
            merged_revision = _merged_revision(conn, project_id, episode.graph_target.key)
            if branch and int(branch[0]) > merged_revision:
                active[episode.episode_id] |= bool(json.loads(branch[1]).get("needs_action"))
    return episodes, active
