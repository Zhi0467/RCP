"""Data-directory-owned notification reconciliation and desktop delivery."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

if TYPE_CHECKING:
    from rcp.background import StartupEffectFence
    from rcp.core.materialize import MaterializationResult

from rcp import web_push
from rcp.core.attention import project_graph_attention
from rcp.core.models import GraphState, Patch
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
        resolve: web_push.Resolver | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        # Tests replace DNS and the HTTP transport; production uses neither.
        self.resolve = resolve or web_push.resolve_host
        self.transport = transport
        self.store = store
        self.catalog = catalog
        self.admission = admission
        self.startup_effect_fence = startup_effect_fence
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        # Guards only the dirty set, so an accepted transition's signal never
        # waits behind a pass's remote reads or push requests.
        self._dirty_lock = threading.Lock()
        # None means every project: a new owner has not reconciled any yet.
        # A project leaves the set only after its reconciliation succeeds.
        self._dirty: set[str] | None = None
        # Projects a running pass took from the dirty set but has not yet
        # reconciled; pulls hold their graph items too.
        self._reconciling: set[str] = set()
        # Lowest accepted main revision signalled per project since its last
        # successful reconciliation. A silent first baseline taken at or past
        # it would swallow attention accepted after the API began serving.
        self._signalled: dict[str, int] = {}
        # Upper revision of attention still swallowed by this process's silent
        # first baseline. Late signals backfill it without rewinding the marker.
        self._first_baselines: dict[str, int] = {}

    def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        # Reconcile immediately in the sender thread: remote graph reads must
        # not delay API readiness. Dirty projects remain held during the pass.
        self._wake.set()
        self._thread = threading.Thread(target=self._run, name="rcp-notifications", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = NOTIFICATION_STOP_TIMEOUT_SECONDS) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _mark_dirty(self, project_id: str) -> None:
        with self._dirty_lock:
            if self._dirty is not None:
                self._dirty.add(project_id)

    def signal(self, project_id: str, revision: int | None = None) -> None:
        """Wake the sender for an accepted main transition at `revision`."""
        with self._dirty_lock:
            if self._dirty is not None:
                self._dirty.add(project_id)
            if revision is not None:
                self._signalled[project_id] = min(
                    self._signalled.get(project_id, revision), revision
                )
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(NOTIFICATION_RECHECK_SECONDS)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self._run_pass(interruptible=True)
            except MaintenanceAdmissionClosed:
                return
            except Exception:
                _LOG.exception("Notification reconciliation pass failed")

    def run_pass(self) -> None:
        """Reconcile, qualify the outbox, and send phone notifications."""
        self._run_pass(interruptible=False)

    def _run_pass(self, *, interruptible: bool) -> None:
        # Only the sender thread yields to stop() between projects; a direct
        # pass always completes.
        if self.startup_effect_fence is not None:
            self.startup_effect_fence.require_open("notification reconciliation")
        with self.admission.mutation("notification reconciliation"), self._lock:
            projects = self.store.projects()
            with self._dirty_lock:
                dirty = self._dirty
                self._dirty = set()
                self._reconciling = (
                    {project.project_id for project in projects} if dirty is None else set(dirty)
                )
            active = [project for project in projects if project.retired_at is None]
            # Episode health is local. Observe it for every project before any
            # graph replay, so one slow remote cannot delay another project's
            # episode baseline or observation.
            for project in active:
                try:
                    self._recheck_episodes(project)
                except Exception:
                    _LOG.exception(
                        "Could not recheck episode notifications for %s", project.project_id
                    )
            # Replaying history is costly, and remote state reads cross SSH,
            # so only projects with an accepted change or a failed pass replay.
            pending = [
                project.project_id
                for project in active
                if dirty is None or project.project_id in dirty
            ]
            while pending:
                if interruptible and self._stop.is_set():
                    # An update boundary is waiting on this pass. Projects it
                    # did not reach stay dirty and held for the next owner.
                    with self._dirty_lock:
                        self._dirty.update(pending)
                        self._reconciling = set()
                    return
                project_id = pending.pop(0)
                try:
                    self.reconcile_project(project_id)
                except Exception:
                    self._mark_dirty(project_id)
                    # Inaccessible canonical state is not empty attention.
                    _LOG.warning(
                        "Could not reconcile graph notifications for %s",
                        project_id,
                        exc_info=True,
                    )
                finally:
                    with self._dirty_lock:
                        self._reconciling.discard(project_id)
            with self._dirty_lock:
                self._reconciling = set()
            self.store.prune_notification_devices()
            self.store.expire_notification_receipts(
                (
                    datetime.fromisoformat(self.store.now())
                    - timedelta(seconds=NOTIFICATION_TTL_SECONDS)
                ).isoformat()
            )
            for row in self.store.pending_notification_rows():
                self.store.guard_notification_delivery(row["device_id"], row["notification_id"])
            self._deliver_web_push()

    def reconcile_project(self, project_id: str) -> None:
        # The marker advances even when nobody wants graph notifications, so
        # turning a kind back on never replays old attention as new.
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
            # Read after the graph, so every transition signalled before the
            # read completed is known to this pass.
            with self._dirty_lock:
                signalled = self._signalled.get(project_id)
            marker = self.store.notification_graph_marker(project_id)
            first_baseline = self._first_baselines.get(project_id)
            if marker is None:
                marker = self._baseline(project_id, replay, patches, signalled)
            if marker["revision"] > replay.state.revision:
                raise RuntimeError("graph is behind its notification marker")
            backfill = []
            baseline_before = None
            for boundary, (before, patch, _) in zip(boundaries, patches, strict=True):
                swallowed = (
                    signalled is not None
                    and first_baseline is not None
                    and signalled <= boundary.revision <= first_baseline
                )
                if boundary.revision <= marker["revision"] and not swallowed:
                    continue
                previous = _attention(before)
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
                if swallowed:
                    if baseline_before is None:
                        baseline_before = before.revision
                    backfill.extend(notifications)
                else:
                    self.store.consume_notification_graph_boundary(
                        project_id,
                        "main",
                        boundary.revision,
                        boundary.transition_id,
                        current,
                        notifications,
                    )
            if baseline_before is not None:
                # A newer signal may already have advanced the marker and sent
                # attention. Recover only the silent prefix; never replay those
                # delivered boundaries or roll current attention backwards.
                self.store.backfill_notification_graph_attention(backfill)
                self._first_baselines[project_id] = baseline_before
            with self._dirty_lock:
                # A signal that arrived during this pass may name a revision the
                # read did not see, or one this baseline swallowed; keep it.
                if self._signalled.get(project_id) == signalled:
                    self._signalled.pop(project_id, None)

    def _baseline(
        self,
        project_id: str,
        replay: MaterializationResult,
        patches: list[tuple[GraphState, Patch, GraphState]],
        signalled: int | None,
    ) -> dict[str, Any]:
        """Persist the silent first baseline and return it as a marker.

        The baseline is the graph as it stood before the API began serving.
        An accepted transition signalled before the baseline is taken places
        the baseline before that revision. Signals arriving later recover the
        swallowed prefix separately, without rewinding an advanced marker.
        A signalled revision the read did not reach is not in this baseline
        either; the next pass delivers it from the marker.
        """
        state = replay.state
        if signalled is not None:
            for before, patch, _ in patches:
                if patch.revision == signalled:
                    state = before
                    break
        attention = _attention(state)
        self.store.consume_notification_graph_boundary(
            project_id, "main", state.revision, None, attention, []
        )
        self._first_baselines[project_id] = state.revision
        return {"revision": state.revision, "attention_json": json.dumps(attention)}

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
        # No pass lock: leasing is atomic in SQLite, so a pull never waits for
        # a pass's remote replay or push requests.
        with self.admission.mutation("desktop notification delivery"):
            return [
                {
                    key: row[key]
                    for key in ("notification_id", "reason", "project_name", "deep_link")
                }
                for row in self._lease_due(device_id)
            ]

    def _deliver_web_push(self) -> None:
        devices = {row["device_id"] for row in self.store.pending_notification_rows()} & {
            device["device_id"]
            for device in self.store.notification_devices()
            if device["kind"] == "web_push"
        }
        if not devices:
            return
        key = web_push.VapidKey.from_pem(self.store.notification_vapid_key())
        for device_id in sorted(devices):
            # Leasing is lazy, so each row is requalified just before its send,
            # and the subscription is read again in case the phone was removed.
            for row in self._lease_due(device_id):
                subscription = self.store.web_push_subscription(device_id)
                if subscription is None:
                    break
                self._send_web_push(key, device_id, subscription, row)

    def _send_web_push(
        self,
        key: web_push.VapidKey,
        device_id: str,
        subscription: dict[str, object],
        row: dict[str, object],
    ) -> None:
        now = datetime.fromisoformat(self.store.now())
        age = (now - datetime.fromisoformat(str(row["created_at"]))).total_seconds()
        payload = {
            "notification_id": row["notification_id"],
            "reason": row["reason"],
            "project_name": row["project_name"],
        }
        # A notify-only phone has no read access, so it gets nothing to open.
        if subscription["opens_links"]:
            payload["deep_link"] = row["deep_link"]
        try:
            result = web_push.send(
                web_push.Subscription(
                    endpoint=str(subscription["endpoint"]),
                    p256dh=str(subscription["p256dh"]),
                    auth=str(subscription["auth"]),
                    origin=str(subscription["origin"]),
                ),
                payload,
                key=key,
                topic=str(row["notification_id"]),
                ttl_seconds=int(NOTIFICATION_TTL_SECONDS - age),
                resolve=self.resolve,
                transport=self.transport,
            )
        except web_push.WebPushRefused:
            _LOG.warning("A phone subscription is no longer an allowed push destination")
            self.store.fail_notification(device_id, str(row["notification_id"]))
            return
        notification_id = str(row["notification_id"])
        if result.outcome == "posted":
            self.store.acknowledge_notification(device_id, notification_id, posted=True)
        elif result.outcome == "gone":
            self.store.delete_notification_device(device_id)
        elif result.outcome == "failed":
            self.store.fail_notification(device_id, notification_id)
        elif result.retry_after_seconds is not None:
            self.store.defer_notification(
                device_id,
                notification_id,
                (now + timedelta(seconds=result.retry_after_seconds)).isoformat(),
            )

    def send_test(self, device_id: str) -> str:
        """Send one test push now; its outcome sets the device status."""
        subscription = self.store.web_push_subscription(device_id)
        if subscription is None:
            return "gone"
        key = web_push.VapidKey.from_pem(self.store.notification_vapid_key())
        try:
            result = web_push.send(
                web_push.Subscription(
                    endpoint=str(subscription["endpoint"]),
                    p256dh=str(subscription["p256dh"]),
                    auth=str(subscription["auth"]),
                    origin=str(subscription["origin"]),
                ),
                {"reason": "test"},
                key=key,
                topic="test",
                ttl_seconds=60,
                resolve=self.resolve,
                transport=self.transport,
            )
        except web_push.WebPushRefused:
            result = web_push.SendResult("failed")
        if result.outcome == "gone":
            self.store.delete_notification_device(device_id)
        else:
            self.store.set_notification_device_status(
                device_id, "on" if result.outcome == "posted" else "delivery_failed"
            )
        return result.outcome

    def _lease_due(self, device_id: str) -> Iterator[dict[str, object]]:
        """Drop items that no longer qualify, then lease the rest with backoff.

        Each row is qualified and leased only when the caller asks for it.
        """
        for row in self.store.pending_notification_rows(device_id):
            now = datetime.fromisoformat(self.store.now())
            notification_id = row["notification_id"]
            if not self.store.guard_notification_delivery(device_id, notification_id):
                continue
            if (
                now - datetime.fromisoformat(row["created_at"])
            ).total_seconds() >= NOTIFICATION_TTL_SECONDS:
                self.store.drop_notification(device_id, notification_id)
                continue
            if row["kind"] in _GRAPH_KINDS:
                # A dirty project's marker may predate an accepted change that
                # resolved this item; hold it until reconciliation runs.
                with self._dirty_lock:
                    held = (
                        self._dirty is None
                        or row["project_id"] in self._dirty
                        or row["project_id"] in self._reconciling
                    )
                if held:
                    continue
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
            yield row
