"""Pull-only, commit-ordered research digest; canonical history is read only."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from urllib.parse import quote

from rcp.core.attention import project_graph_attention
from rcp.core.models import GraphBranchMetadata, GraphState, Patch
from rcp.episode_health import episode_needs_action, load_episode_health
from rcp.history.delta import semantic_delta
from rcp.limits import DIGEST_RECHECK_SECONDS, DIGEST_STOP_TIMEOUT_SECONDS
from rcp.server_ops.maintenance import MaintenanceAdmissionClosed
from rcp.storage.digest import append_digest_event
from rcp.transport import StateUnavailable

_LOG = logging.getLogger(__name__)
_RAN = {
    "episode_ended",
    "job_ended",
    "task_failed",
    "consolidation_report",
    "consolidation_failed",
    "episode_report",
}


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


# Sources whose edits are the viewer's own when the viewer is their actor: a
# member's direct edits and the Work turns of chats that member ran.
OWN_SOURCES = frozenset({"member", "chat"})


def attribution(store, project_id: str, patch: Patch) -> dict:
    """Resolve source once, with explicit provenance ahead of producer fallbacks."""
    result = {"actor_user_id": None, "report_artifact_id": None, "deep_link": None}
    if patch.branch_merge is not None:
        episode_id = patch.branch_merge.episode_id
        kind, key, label = "episode", episode_id, _episode_title(store, episode_id)
        result["deep_link"] = deep_link(project_id, "main", "episode", episode_id)
    else:
        operation_id = patch.source_operation_id or patch.task_id
        consolidation = (
            store.consolidation_run_for_operation(operation_id) if operation_id else None
        )
        if consolidation is not None:
            kind, key, label = "consolidation", consolidation.run_id, "Nightly consolidation"
            result["report_artifact_id"] = consolidation.report_artifact_id
            result["deep_link"] = deep_link(project_id, "main", "consolidation", key)
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
                # An agent in a member's own chat acts as that member (invariant 3).
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
    store, project_id: str, target: str, before: GraphState, patch: Patch, after: GraphState
) -> dict:
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
    kind = "graph_change"
    if target != "main":
        kind = "branch_change"
        episode_id = target.removeprefix("branch:")
        payload.update(
            episode_id=episode_id,
            title=_episode_title(store, episode_id),
            deep_link=deep_link(project_id, target, "episode", episode_id),
            needs_action=any(kind == "decision" for kind, _ in new),
        )
        payload["attention"] = []
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


def assemble_digest(
    project_id: str,
    user_id: str,
    events: list[dict],
    mark: dict | None,
    cursor: int,
    state: GraphState | None = None,
) -> dict:
    """The sole rendered-line grouping function, also used by landing counts."""
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
    attention, latest_nodes, groups, branches, ran = {}, {}, {}, {}, {}
    node_edits = {}
    merged = {}
    for event in events:
        if "merged_branch_revision" in event["payload"]:
            episode_id = event["source_key"].removeprefix("episode:")
            merged[episode_id] = max(
                merged.get(episode_id, 0), event["payload"]["merged_branch_revision"]
            )
    for event in events:
        payload = event["payload"]
        kind, item_id, target = event["kind"], event["item_id"], event["target"]
        if kind == "graph_change":
            for entry in payload.get("attention", []):
                identity = (target, entry["kind"], entry["item_id"])
                attention[identity] = dict(
                    entry,
                    target=target,
                    created_at=event["created_at"],
                    deep_link=deep_link(project_id, target, entry["kind"], entry["item_id"]),
                )
            if event["actor_user_id"] == user_id and payload["source_kind"] in OWN_SOURCES:
                continue
            if not payload["edits"]:
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
                    report_artifact_id=payload.get("report_artifact_id"),
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
        elif kind == "branch_change":
            if event["actor_user_id"] == user_id and payload.get("source_kind") in OWN_SOURCES:
                continue
            episode_id = payload["episode_id"]
            if payload["edits"] and int(item_id) > merged.get(episode_id, -1):
                branch = branches.setdefault(
                    episode_id,
                    dict(
                        episode_id=episode_id,
                        title=payload["title"],
                        edits=0,
                        deep_link=payload.get("deep_link"),
                    ),
                )
                branch["edits"] += payload["edits"]
        elif kind in {"question_attention", "episode_attention"}:
            item_kind = "question" if kind == "question_attention" else "episode"
            attention[(target, item_kind, item_id)] = dict(
                kind=item_kind,
                item_id=item_id,
                target=target,
                title=payload["title"],
                active=payload["active"],
                created_at=event["created_at"],
                deep_link=payload.get("deep_link"),
            )
        elif kind in _RAN:
            if kind == "consolidation_report":
                group = groups.get("consolidation:" + item_id)
                if group is not None:
                    group["report_artifact_id"] = payload.get("report_artifact_id")
            ran[(kind, item_id)] = dict(
                kind=kind,
                item_id=item_id,
                title=payload["title"],
                status=payload.get("status"),
                deep_link=payload.get("deep_link"),
                created_at=event["created_at"],
            )
    for node_id, key in sorted(latest_nodes.items()):
        groups[key]["node_ids"].append(node_id)
        groups[key]["edits"] += node_edits.get((key, node_id), 0)
    needs = []
    for item in attention.values():
        if not item.pop("active"):
            continue
        needs.append(item)
    changed_ids = sorted(
        node_id for node_id in latest_nodes if state is None or node_id in state.nodes
    )
    changed, branch_lines, ran_lines = (
        [group for group in groups.values() if group["edits"] or group["node_ids"]],
        list(branches.values()),
        list(ran.values()),
    )
    return dict(
        cursor=cursor,
        mark=mark,
        needs_you=needs,
        changed=changed,
        branches=branch_lines,
        ran=ran_lines,
        changed_node_ids=changed_ids,
        count=len(needs) + len(changed) + len(branch_lines) + len(ran_lines),
    )


def read_digest(store, catalog, project_id: str, user_id: str) -> dict:
    mark, cursor, events = store.digest_snapshot(project_id, user_id)
    if not events:
        return assemble_digest(project_id, user_id, events, mark, cursor)
    state = GraphState()
    if any(event["kind"] == "graph_change" for event in events):
        service = catalog.loaded_service(project_id)
        if service is not None:
            with service.history.workspace.snapshot_lock:
                state = GraphState.model_validate_json(
                    (service.history.root / "graph.json").read_text()
                )
        else:
            snapshot = catalog.cached_snapshot(project_id)
            if snapshot is None:
                raise StateUnavailable("The project graph snapshot is not available yet.")
            state = GraphState.model_validate(snapshot["graph"])
    return _assemble_current(store, project_id, user_id, events, mark, cursor, state)


def _assemble_current(store, project_id, user_id, events, mark, cursor, state=None):
    result = assemble_digest(project_id, user_id, events, mark, cursor, state)
    question_ids = [item["item_id"] for item in result["needs_you"] if item["kind"] == "question"]
    with store.connection() as conn:
        open_questions = (
            {
                row[0]
                for row in conn.execute(
                    "SELECT question_id FROM questions WHERE state='pending' AND withdrawn_readonly=0 "
                    "AND question_id IN (SELECT value FROM json_each(?))",
                    (json.dumps(question_ids),),
                )
            }
            if question_ids
            else set()
        )
    episode_ids = [item["item_id"] for item in result["needs_you"] if item["kind"] == "episode"]
    _, active = _episode_attention(store, project_id, episode_ids)
    result["needs_you"] = [
        item
        for item in result["needs_you"]
        if (item["kind"] != "question" or item["item_id"] in open_questions)
        and (item["kind"] != "episode" or active.get(item["item_id"], False))
    ]
    result["count"] = sum(len(result[key]) for key in ("needs_you", "changed", "branches", "ran"))
    return result


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
        "AND source_key=? ORDER BY seq DESC LIMIT 1",
        (project_id, "episode:" + target.removeprefix("branch:")),
    ).fetchone()
    return json.loads(row[0]).get("merged_branch_revision", -1) if row else -1


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
        with self._dirty_lock:
            if self._dirty is not None:
                self._dirty.add((project_id, target))
        self._wake.set()

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
                    self.signal(project_id)
                    _LOG.exception("Digest reconciliation failed for %s", project_id)

    def _branch_targets(self, project_id):
        with self.store.connection() as conn:
            return {
                episode.graph_target.key: episode.graph_target.branch_id
                for episode in self.store.episodes(project_id, limit=None)
                if episode.graph_target.kind == "branch"
                and (
                    episode.status in {"queued", "running", "stopping", "wrapping_up"}
                    or _merged_revision(conn, project_id, episode.graph_target.key) < 0
                )
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
                    if metadata.head.revision > metadata.base_head.revision
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
            history = self.catalog.open(project_id).history
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
                self._project_history(project_id, "main", history)
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
                    self._project_history(
                        project_id, target, history.branch(branch_id, initialize=False)
                    )
                except Exception:
                    _LOG.exception("Digest branch projection failed for %s/%s", project_id, target)
            if initial and (targets is None or "main" in targets):
                # Episodes too: a mark made before their baseline would hide
                # an episode that starts needing the human in that gap.
                self._observe_episodes(project_id)
                self._project_history(project_id, "main", history)

    def _project_history(self, project_id, target, history):
        with self.store.connection() as conn:
            head = conn.execute(
                "SELECT revision,patch_id FROM digest_heads WHERE project_id=? AND target=?",
                (project_id, target),
            ).fetchone()
        replay, boundaries = history.accepted_patch_boundaries()
        if replay.state.replay_status != "complete":
            raise ValueError("digest requires a complete accepted history")
        identities = {}
        prefix = hashlib.sha256()
        for _, patch, _ in boundaries:
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
            if reset
            else [
                graph_event(self.store, project_id, target, before, patch, after)
                for before, patch, after in boundaries
                if patch.revision > merged_revision
                and (
                    (head is not None and patch.revision > head["revision"])
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
        for episode in episodes:
            current, _, _, blocked = health[episode.episode_id]
            active[episode.episode_id] = episode_needs_action(current, blocked)
            if episode.graph_target.kind != "branch":
                continue
            branch = conn.execute(
                "SELECT item_id,payload_json FROM digest_events WHERE project_id=? AND kind='branch_change' AND target=? ORDER BY seq DESC LIMIT 1",
                (project_id, episode.graph_target.key),
            ).fetchone()
            merged_revision = _merged_revision(conn, project_id, episode.graph_target.key)
            if branch and int(branch[0]) > merged_revision:
                active[episode.episode_id] |= bool(json.loads(branch[1]).get("needs_action"))
    return episodes, active
