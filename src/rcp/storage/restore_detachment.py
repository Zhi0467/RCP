"""One offline transaction that detaches restored operational state."""

from __future__ import annotations

import json
import uuid
from collections.abc import Collection

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
        restored_memory_projects: Collection[str] = (),
    ) -> None:
        """Make all captured continuations historical before ordinary startup.

        A memory migration marker survives only for a project whose local memory
        folder came back with the archive; re-migration is create-if-absent.
        """

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
            # A done one-shot stays done (it cannot be re-enabled); only its captured wake goes.
            connection.execute(
                "UPDATE agent_schedules SET state='disabled', disabled_reason='restore', "
                "pending_since=NULL, next_fire_at=NULL WHERE state IN ('active','disabled')"
            )
            connection.execute("UPDATE agent_schedules SET pending_since=NULL WHERE state='done'")
            connection.execute(
                "UPDATE approval_cards SET expires_at=? "
                "WHERE running_since IS NULL AND result_json IS NULL AND expires_at > ?",
                (now, now),
            )
            # Undelivered handoffs would launch project turns on the restored host,
            # and a delivered one would wake its Space thread once it settles.
            connection.execute("DELETE FROM agent_handoffs WHERE target_operation_id IS NULL")
            connection.execute(
                "UPDATE agent_handoffs SET notified_operation_id=target_operation_id "
                "WHERE notified_operation_id IS NULL"
            )
            # A remote base's memory is never in the archive, so its marker always goes.
            connection.execute(
                "DELETE FROM memory_migrations WHERE project_id NOT IN "
                "(SELECT value FROM json_each(?)) OR project_id IN "
                "(SELECT project_id FROM projects WHERE state_remote=1)",
                (json.dumps(sorted(set(restored_memory_projects))),),
            )
            connection.execute("DELETE FROM notification_devices")
            connection.execute("DELETE FROM notification_phone_pairings")
            connection.execute("DELETE FROM notification_graph_markers")
            connection.execute("DELETE FROM notification_episode_observations")
            connection.execute("DELETE FROM notification_project_baselines")
            # Pending memory cleanup names the live host's paths; a restored copy never acts on it.
            connection.execute("DELETE FROM memory_deletions")
            self.detach_space_authentication_for_restore(connection, now=now)
            self.detach_provider_logins_for_restore(connection, now=now)
