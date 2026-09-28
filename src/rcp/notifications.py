"""Data-directory-owned notification reconciliation and desktop delivery."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from rcp.background import StartupEffectFence

from rcp.core.attention import project_graph_attention
from rcp.core.models import GraphState
from rcp.episode_health import load_episode_health
from rcp.limits import (
    NOTIFICATION_RECHECK_SECONDS,
    NOTIFICATION_RETRY_BASE_SECONDS,
    NOTIFICATION_RETRY_MAX_SECONDS,
    NOTIFICATION_STOP_TIMEOUT_SECONDS,
    NOTIFICATION_TTL_SECONDS,
)
from rcp.projects import ProjectCatalog
from rcp.runs.transition_event_reconciliation import AcceptedGraphBoundary
from rcp.server_ops.maintenance import MaintenanceAdmissionClosed, RuntimeAdmissionGate
from rcp.storage import AppStore, ProjectRecord

_LOG = logging.getLogger(__name__)
_TERMINAL = {"completed", "stopped", "failed"}
_GRAPH_KINDS = {"proposal", "decision", "blocker"}


def _attention(state: GraphState) -> dict[str, list[str]]:
    attention = project_graph_attention(state)
    return {
        "proposal": attention.pending_proposal_ids,
        "decision": attention.decisions_awaiting_choice_ids,
        "blocker": attention.open_blocker_ids,
    }


def _notification(
    project: ProjectRecord,
    *,
    target: str,
    kind: str,
    item_id: str,
    occurrence: object,
    reason: str,
    created_at: str | None = None,
) -> dict[str, object]:
    identity = json.dumps([project.project_id, target, kind, item_id, occurrence])
    route = "episode" if kind.startswith("episode_") else kind
    return {
        "created_at": created_at,
        "notification_id": hashlib.sha256(identity.encode()).hexdigest()[:32],
        "project_id": project.project_id,
        "target": target,
        "kind": kind,
        "item_id": item_id,
        "reason": reason,
        "project_name": project.name,
        "deep_link": (
            f"#/projects/{quote(project.project_id, safe='')}/targets/{quote(target, safe='')}"
            f"/{route}/{quote(item_id, safe='')}"
        ),
    }


class NotificationSender:
    """Own one interruptible loop; desktop submission is acknowledged by the shell."""

    def __init__(
        self,
        store: AppStore,
        catalog: ProjectCatalog,
        *,
        admission: RuntimeAdmissionGate,
        startup_effect_fence: StartupEffectFence | None = None,
    ) -> None:
        self.store = store
        self.catalog = catalog
        self.admission = admission
        self.startup_effect_fence = startup_effect_fence
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        # None means every project: a new owner has not reconciled any yet.
        # A project leaves the set only after its reconciliation succeeds.
        self._dirty: set[str] | None = None

    def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="rcp-notifications", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = NOTIFICATION_STOP_TIMEOUT_SECONDS) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def signal(self, project_id: str) -> None:
        with self._lock:
            if self._dirty is not None:
                self._dirty.add(project_id)
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(NOTIFICATION_RECHECK_SECONDS)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.run_pass()
            except MaintenanceAdmissionClosed:
                return
            except Exception:
                _LOG.exception("Notification reconciliation pass failed")

    def run_pass(self) -> None:
        if self.startup_effect_fence is not None:
            self.startup_effect_fence.require_open("notification reconciliation")
        with self.admission.mutation("notification reconciliation"), self._lock:
            dirty = self._dirty
            self._dirty = set()
            for project in self.store.projects():
                if project.retired_at is not None:
                    continue
                try:
                    self._recheck_episodes(project)
                except Exception:
                    _LOG.exception(
                        "Could not recheck episode notifications for %s", project.project_id
                    )
                # Replaying history is costly, and remote state reads cross SSH,
                # so only projects with an accepted change or a failed pass replay.
                if dirty is not None and project.project_id not in dirty:
                    continue
                try:
                    self.reconcile_project(project.project_id)
                except Exception:
                    self._dirty.add(project.project_id)
                    # Inaccessible canonical state is not empty attention.
                    _LOG.warning(
                        "Could not reconcile graph notifications for %s",
                        project.project_id,
                        exc_info=True,
                    )
            self.store.prune_notification_devices()
            for row in self.store.pending_notification_rows():
                self.store.guard_notification_delivery(row["device_id"], row["notification_id"])

    def reconcile_project(self, project_id: str) -> None:
        if not self.store.notification_graph_enabled(project_id):
            return
        project = next(
            (item for item in self.store.projects() if item.project_id == project_id), None
        )
        if project is None:
            return
        with self._lock:
            service = self.catalog.open(project_id)
            replay, patches = service.history.accepted_patch_boundaries()
            boundaries = [AcceptedGraphBoundary.from_replay(*patch) for patch in patches]
            if any(boundary.target.kind != "main" for boundary in boundaries):
                raise RuntimeError("main notification history contains a different target")
            if replay.state.replay_status != "complete":
                raise RuntimeError("notification graph replay is incomplete")
            marker = self.store.notification_graph_marker(project_id)
            if marker is None:
                self.store.consume_notification_graph_boundary(
                    project_id,
                    "main",
                    replay.state.revision,
                    None,
                    _attention(replay.state),
                    [],
                )
                return
            if marker["revision"] > replay.state.revision:
                raise RuntimeError("graph is behind its notification marker")
            previous = json.loads(marker["attention_json"])
            for boundary, (_, patch, _) in zip(boundaries, patches, strict=True):
                if boundary.revision <= marker["revision"]:
                    continue
                current = _attention(boundary.state)
                notifications = [
                    _notification(
                        project,
                        target="main",
                        kind=kind,
                        item_id=item_id,
                        occurrence=boundary.transition_id or boundary.revision,
                        reason=kind,
                        created_at=patch.created_at.isoformat(),
                    )
                    for kind, ids in current.items()
                    for item_id in sorted(set(ids) - set(previous.get(kind, [])))
                ]
                self.store.consume_notification_graph_boundary(
                    project_id,
                    "main",
                    boundary.revision,
                    boundary.transition_id,
                    current,
                    notifications,
                )
                previous = current

    def _recheck_episodes(self, project: ProjectRecord) -> None:
        baseline_at = self.store.now()
        episodes = self.store.episodes(project.project_id, limit=None)
        observations = self.store.notification_episode_observations(project.project_id)
        baseline = self.store.notification_project_baseline(project.project_id)
        selected = [
            episode
            for episode in episodes
            if baseline is None
            or episode.ended_at is None
            or observations.get(episode.episode_id, {}).get("health") not in _TERMINAL
        ]
        healths = load_episode_health(self.store, selected)
        if baseline is None:
            self.store.baseline_notification_project(
                project.project_id,
                [
                    (
                        episode.episode_id,
                        healths[episode.episode_id][0],
                        healths[episode.episode_id][3],
                    )
                    for episode in selected
                ],
                baseline_at=baseline_at,
            )
            return
        for episode in selected:
            health, _, _, blocked = healths[episode.episode_id]
            prior = observations.get(episode.episode_id)
            if prior is not None and (prior["health"], prior["blocked_reason"]) == (
                health,
                blocked,
            ):
                continue
            kind = None
            if health == "needs_action" or (health == "wrapping_up" and blocked == "sign_in"):
                kind = "episode_needs_action"
            elif health in _TERMINAL:
                kind = "episode_finished"
            notification = None
            if kind is not None:
                notification = _notification(
                    project,
                    target=episode.graph_target.key,
                    kind=kind,
                    item_id=episode.episode_id,
                    # Observation and outbox commit together. A fresh event id
                    # distinguishes repeated health cycles even if clocks repeat;
                    # all delivery retries reuse the committed outbox id.
                    occurrence=uuid.uuid4().hex,
                    created_at=max(episode.ended_at or episode.updated_at, episode.updated_at)
                    if health in _TERMINAL
                    else None,
                    reason="episode_needs_action"
                    if kind == "episode_needs_action"
                    else "episode_finished",
                )
                notification.update(observed_health=health, observed_blocked_reason=blocked)
            self.store.observe_notification_episode(
                project.project_id,
                episode.episode_id,
                health,
                blocked,
                notification,
            )

    def pending_desktop(self, device_id: str) -> list[dict[str, object]]:
        """Lease due items without marking delivery; a lost acknowledgement retries."""
        if self.startup_effect_fence is not None:
            self.startup_effect_fence.require_open("notification delivery")
        with self.admission.mutation("desktop notification delivery"), self._lock:
            output = []
            now = datetime.fromisoformat(self.store.now())
            for row in self.store.pending_notification_rows(device_id):
                notification_id = row["notification_id"]
                if not self.store.guard_notification_delivery(device_id, notification_id):
                    continue
                if (
                    now - datetime.fromisoformat(row["created_at"])
                ).total_seconds() >= NOTIFICATION_TTL_SECONDS:
                    self.store.drop_notification(device_id, notification_id)
                    continue
                if row["kind"] in _GRAPH_KINDS:
                    # The marker holds the attention of the last reconciled
                    # revision, so a pull never replays canonical history.
                    marker = self.store.notification_graph_marker(row["project_id"])
                    if marker is None:
                        continue
                    attention = json.loads(marker["attention_json"])
                    unresolved = row["item_id"] in attention.get(row["kind"], [])
                else:
                    episode = self.store.episode(row["item_id"])
                    if episode is None:
                        unresolved = False
                    else:
                        health, _, _, blocked = load_episode_health(self.store, [episode])[
                            episode.episode_id
                        ]
                        unresolved = (health, blocked) == (
                            row["observed_health"],
                            row["observed_blocked_reason"],
                        ) and (
                            health in _TERMINAL
                            if row["kind"] == "episode_finished"
                            else health == "needs_action"
                            or (health == "wrapping_up" and blocked == "sign_in")
                        )
                if not unresolved:
                    self.store.drop_notification(device_id, notification_id)
                    continue
                delay = min(
                    NOTIFICATION_RETRY_MAX_SECONDS,
                    NOTIFICATION_RETRY_BASE_SECONDS * 2 ** row["attempts"],
                )
                if not self.store.begin_notification_attempt(
                    device_id, notification_id, (now + timedelta(seconds=delay)).isoformat()
                ):
                    continue
                output.append(
                    {
                        key: row[key]
                        for key in ("notification_id", "reason", "project_name", "deep_link")
                    }
                )
            return output
