"""One offline transaction that detaches restored operational state."""

from __future__ import annotations

import uuid

from rcp.storage.chat_forks import ChatForkStoreMixin
from rcp.storage.mixin_base import StoreMixinBase
from rcp.storage.models import _required_timestamp


class RestoreDetachmentStoreMixin(StoreMixinBase):
    """Compose concrete lifecycle owners at the pre-startup restore boundary."""

    def detach_restored_lifecycle(
        self,
        *,
        diagnostic: str,
        confirmed_by: str,
        detached_at: str | None = None,
    ) -> None:
        """Make all captured continuations historical before ordinary startup."""

        detail = " ".join(diagnostic.split())[:1400]
        confirmer = " ".join(confirmed_by.split())[:400]
        if not detail or not confirmer:
            raise ValueError("restore detachment requires a reason and confirmer")
        recorded_detail = f"{detail} Restore confirmed by {confirmer}."[:2000]
        now = detached_at or self.now()
        _required_timestamp(now)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # A restore request retains detached_at across retries. Keep that
            # operation idempotent while fencing every distinct restore.
            restore_epoch = uuid.uuid5(uuid.NAMESPACE_URL, f"rcp:resource-restore:{now}")
            connection.execute(
                "UPDATE resource_watermark_epoch SET epoch=? WHERE singleton=1",
                (str(restore_epoch),),
            )
            self.detach_agent_tasks_for_restore(connection, diagnostic=recorded_detail, now=now)
            ChatForkStoreMixin.detach_chat_forks_for_restore(connection)
            self.detach_episode_reports_for_restore(connection, diagnostic=recorded_detail, now=now)
            self.detach_experiment_episodes_for_restore(
                connection,
                diagnostic=recorded_detail,
                now=now,
            )
            self.detach_auto_research_for_restore(
                connection,
                diagnostic=recorded_detail,
                now=now,
            )
            self.detach_auto_research_children_for_restore(
                connection,
                diagnostic=recorded_detail,
                confirmed_by=confirmer,
                now=now,
            )
            self.detach_watchers_for_restore(
                connection,
                diagnostic=detail,
                confirmed_by=confirmer,
                now=now,
            )
            self.detach_project_provisioning_for_restore(
                connection,
                diagnostic=recorded_detail,
                now=now,
            )
            self.detach_project_transfers_for_restore(
                connection,
                diagnostic=recorded_detail,
                now=now,
            )
            self.detach_consolidation_for_restore(connection, diagnostic=recorded_detail, now=now)
            connection.execute("DELETE FROM notification_devices")
            connection.execute("DELETE FROM notification_phone_pairings")
            connection.execute("DELETE FROM notification_graph_markers")
            connection.execute("DELETE FROM notification_episode_observations")
            connection.execute("DELETE FROM notification_project_baselines")
            self.detach_space_authentication_for_restore(connection, now=now)
            self.detach_provider_logins_for_restore(connection, now=now)
