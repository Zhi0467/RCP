"""Every stored lifecycle without an explorer, written down.

Each ``Entry`` names a stored state machine, its status (the ``Literal`` type
that holds it, or the column or field name where none exists), the tables it
owns, the product functions that change it, and the rules the code keeps. The
references are imports, so a rename fails collection and basedpyright, and the
writer scan of ``writers.py`` treats an entry as the owner of its tables:
every function the scan finds writing one of them must be in ``writers``.

``NOT_LIFECYCLE`` names every other written table that no explorer owns, with
why it holds no lifecycle. ``UNKNOWN_TABLE`` names every writer of an
interpolated table that no entry declares, with the tables it reaches.

Two entries go beyond the design's twenty: evidence flags and notification
delivery hold a status column with transitions, so neither fits
``NOT_LIFECYCLE``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from rcp.api.team import TeamDevicePairingStatus, TeamInvitationStatus
from rcp.compute_jobs.jobs import launch_compute_job, refresh_compute_job
from rcp.compute_jobs.models import ComputeJobStatus
from rcp.compute_jobs.reconcile import reconcile_compute_jobs
from rcp.conversation_worktrees import (
    admit_conversation_worktree,
    ensure_episode_worktree,
    remove_conversation_worktree,
)
from rcp.core.models import (
    ConversationWorktreeBinding,
    EpisodeIsolationState,
    GraphBranchSummary,
)
from rcp.paper.changes import ChangeGroup, PaperChanges
from rcp.paper.live import PaperLive
from rcp.paper.models import PaperTree
from rcp.projects import ProjectCatalog
from rcp.runs.branch_merge import commit_branch_merge_with_history, stream_branch_merge_run
from rcp.runs.branch_merge_admission import (
    recover_branch_merge_task,
    settle_agentless_merge,
    start_branch_merge,
    start_human_branch_merge,
    start_owned_branch_merge,
)
from rcp.runs.browser_lifecycle import (
    _retry_memory_cleanup,
    close_chat_browser_owners,
    retry_browser_cleanup,
)
from rcp.runs.episodes.merge import (
    cleanup_episode,
    complete_graph_merge,
    merge_episode,
    reconcile_episode_merge,
)
from rcp.runs.project_deletion import _cancel_external_work, stop_project_work
from rcp.runs.provider_login import delete_removed_credential
from rcp.server_ops.application_validation import _rebind_local_stage_paths, build_rehearsal_overlay
from rcp.server_ops.backup_capture import BackupCaptureCoordinator
from rcp.server_ops.backup_paper import restore_paper_section
from rcp.server_ops.cli import ServerEventEmitter
from rcp.server_ops.members import _remove_member_agent_data
from rcp.server_ops.models import ServerStepState
from rcp.server_ops.provider_cleanup import fence_member_provider_logins
from rcp.server_ops.restore import (
    RestoreRepositoryRecovery,
    _recover_repositories,
    detach_restore_database,
    prepare_restore,
)
from rcp.storage.agent_tasks import AgentTaskStoreMixin
from rcp.storage.artifact_imports import ArtifactImportStoreMixin
from rcp.storage.artifacts import ArtifactStoreMixin, insert_artifact
from rcp.storage.auto_research import AutoResearchStoreMixin, migrate_legacy_auto_research
from rcp.storage.auto_research_children import AutoResearchChildrenStoreMixin
from rcp.storage.base import AppStoreBase
from rcp.storage.branch_names import (
    BranchNameStoreMixin,
    _insert_graph_branch,
    backfill_graph_branches,
)
from rcp.storage.chat_display import ChatDisplayStoreMixin
from rcp.storage.chat_forks import ChatForkStoreMixin
from rcp.storage.compute_jobs import ComputeJobStoreMixin
from rcp.storage.consolidation import ConsolidationStoreMixin
from rcp.storage.conversation_worktrees import ConversationWorktreeStoreMixin
from rcp.storage.evidence_flags import (
    EvidenceFlagStoreMixin,
    adopt_legacy_evidence_flag_requests,
    record_evidence_flag_address,
    settle_ended_evidence_flag_addresses,
    settle_evidence_flag_address,
)
from rcp.storage.models import (
    AutoResearchChildExperimentState,
    AutoResearchRecoveryStatus,
    ProjectProvisioningStatus,
    ProjectTransferPhase,
    ProviderLoginStateRecord,
)
from rcp.storage.notifications import NotificationStoreMixin
from rcp.storage.owner_auth import OwnerAuthStoreMixin
from rcp.storage.project_groups import ProjectGroupStoreMixin
from rcp.storage.projects import ProjectStoreMixin
from rcp.storage.provider_logins import ProviderLoginStoreMixin, migrate_per_member_provider_logins
from rcp.storage.provisioning import ProjectProvisioningStoreMixin
from rcp.storage.restore_detachment import RestoreDetachmentStoreMixin
from rcp.storage.spaces import SpaceStoreMixin
from rcp.storage.transfer import ProjectTransferStoreMixin
from rcp.terminals.manager import TerminalManager


@dataclass(frozen=True)
class Rule:
    """A rule the code keeps: one sentence and the spec, invariant or guard stating it."""

    name: str
    statement: str
    source: str


@dataclass(frozen=True)
class Entry:
    """One stored lifecycle, shaped like an explorer machine for the writer scan."""

    name: str
    status: object
    tables: frozenset[str]
    writers: tuple[Callable[..., Any], ...]
    rules: tuple[Rule, ...]

    @property
    def unreached_writers(self) -> Mapping[Callable[..., Any], str]:
        """No action runs a catalog entry, so no writer is unreached."""

        return {}


def _field(model: type[BaseModel], name: str) -> object:
    """The ``Literal`` type of a status field declared inline on a model."""

    return model.model_fields[name].annotation


PROJECT_PROVISIONING = Entry(
    name="project provisioning",
    status=ProjectProvisioningStatus,
    tables=frozenset({"project_provisioning_requests", "project_provisioning_step_receipts"}),
    writers=(
        ProjectProvisioningStoreMixin.create_project_provisioning_request,
        ProjectProvisioningStoreMixin._insert_project_provisioning_request,
        ProjectProvisioningStoreMixin.transition_project_provisioning_request,
        ProjectProvisioningStoreMixin._commit_transfer_provisioning_completion,
        ProjectProvisioningStoreMixin._transition_project_provisioning_to_restore_reentry,
        ProjectProvisioningStoreMixin.detach_project_provisioning_for_restore,
        ProjectStoreMixin.delete_project_records,
    ),
    rules=(
        Rule(
            "transition_matches_revision",
            "A transition commits only from the expected revision and status along an "
            "allowed edge, and writes one step receipt.",
            "code guard at transition_project_provisioning_request",
        ),
        Rule(
            "completion_needs_review_digest",
            "Final completion requires the exact review digest and a current member while "
            "the preparation authorizer remains enrolled.",
            "server spec: Durable project provisioning",
        ),
        Rule(
            "cancel_before_machine_work",
            "A member may cancel a new-project request only before machine work and with "
            "nothing to remove.",
            "server spec: Durable project provisioning",
        ),
    ),
)

PROJECT_TRANSFER = Entry(
    name="project transfer",
    status=ProjectTransferPhase,
    tables=frozenset(
        {
            "project_transfer_requests",
            "project_transfer_proofs",
            "project_transfer_uploads",
            "project_transfer_imports",
            "project_transfer_import_configurations",
            "project_transfer_activations",
            "project_transfer_restore_reentries",
        }
    ),
    writers=(
        ProjectProvisioningStoreMixin.create_source_project_transfer_request,
        ProjectProvisioningStoreMixin.create_target_project_transfer_request,
        ProjectProvisioningStoreMixin._insert_project_transfer_request,
        ProjectProvisioningStoreMixin._replace_project_transfer_record,
        ProjectProvisioningStoreMixin.link_source_project_transfer_request,
        ProjectProvisioningStoreMixin.record_target_project_transfer_admission,
        ProjectProvisioningStoreMixin.accept_target_project_transfer_admission,
        ProjectProvisioningStoreMixin.record_source_project_transfer_release,
        ProjectProvisioningStoreMixin.accept_source_project_transfer_release,
        ProjectProvisioningStoreMixin.mark_source_project_transfer_fenced,
        ProjectProvisioningStoreMixin.bind_project_transfer_archive,
        ProjectProvisioningStoreMixin.begin_target_project_transfer_upload,
        ProjectProvisioningStoreMixin.complete_target_project_transfer_upload,
        ProjectProvisioningStoreMixin._invalidate_project_transfer_upload,
        ProjectProvisioningStoreMixin.activate_target_project_transfer,
        ProjectProvisioningStoreMixin.verify_target_project_transfer_activation,
        ProjectProvisioningStoreMixin.expose_project_transfer_proof,
        ProjectProvisioningStoreMixin.acknowledge_project_transfer_proof,
        ProjectProvisioningStoreMixin.consume_project_transfer_proof,
        ProjectProvisioningStoreMixin.accept_project_transfer_cleanup_acknowledgment,
        ProjectProvisioningStoreMixin.complete_project_transfer_request,
        ProjectProvisioningStoreMixin.reenter_restored_target_project_transfer,
        ProjectProvisioningStoreMixin.retire_source_project_transfer,
        ProjectProvisioningStoreMixin.detach_project_transfers_for_restore,
        ProjectTransferStoreMixin.begin_project_transfer_import,
        ProjectTransferStoreMixin.complete_project_transfer_import,
        ProjectStoreMixin.delete_project_records,
    ),
    rules=(
        Rule(
            "phases_are_linear",
            "Each transfer step commits only from the phase directly before it.",
            "code guard at link_source_project_transfer_request",
        ),
        Rule(
            "target_admits_before_source_releases",
            "Final review records target admission before source release, and target "
            "confirmation alone creates nothing and leaves the source writable.",
            "server spec: Durable project provisioning",
        ),
        Rule(
            "proofs_reveal_after_fence",
            "The source reveals its proof only in the sealed post-fence archive and the "
            "target reveals its proof only after activation.",
            "server spec: Durable project provisioning",
        ),
    ),
)

PROVIDER_LOGIN = Entry(
    name="provider login",
    status=_field(ProviderLoginStateRecord, "state"),
    tables=frozenset({"provider_login_states", "provider_login_assignments"}),
    writers=(
        ProviderLoginStoreMixin.add_provider_login,
        ProviderLoginStoreMixin.mark_provider_login_failed,
        ProviderLoginStoreMixin.mark_provider_login_verified,
        ProviderLoginStoreMixin.mark_provider_login_signed_out,
        ProviderLoginStoreMixin.set_provider_login_draining,
        ProviderLoginStoreMixin.finish_provider_login_removal,
        ProviderLoginStoreMixin._write_provider_login_state,
        ProviderLoginStoreMixin.assign_provider_login,
        ProviderLoginStoreMixin.set_provider_login_limit,
        ProviderLoginStoreMixin.detach_provider_logins_for_restore,
        migrate_per_member_provider_logins,
        delete_removed_credential,
        fence_member_provider_logins,
    ),
    rules=(
        Rule(
            "launch_generation_revokes",
            "Only the generation captured at launch can revoke a login, so a late failure "
            "cannot revoke a repaired login.",
            "providers spec: Login state and failure",
        ),
        Rule(
            "one_own_login",
            "An own login is unique per provider, host and member while it is not removed.",
            "providers spec: Login state and failure",
        ),
        Rule(
            "draining_admits_no_new_choice",
            "A draining login admits no new choices but stays usable by lineage work until "
            "its running turns end.",
            "providers spec: Login state and failure",
        ),
    ),
)

MEMBER_REMOVAL = Entry(
    name="member removal",
    status="removed_at",
    tables=frozenset({"space_users"}),
    writers=(
        SpaceStoreMixin.begin_member_removal,
        SpaceStoreMixin.complete_member_removal,
        SpaceStoreMixin.enroll_team_member,
        SpaceStoreMixin.preprovision_team_member,
        SpaceStoreMixin.rename_space_user,
        AppStoreBase._insert_local_owner,
        _remove_member_agent_data,
    ),
    rules=(
        Rule(
            "preview_boundary_matches",
            "Removal begins only while the reviewed preview boundary still matches.",
            "code guard at begin_member_removal",
        ),
        Rule(
            "removal_keeps_a_member",
            "Removal never leaves no member able to sign in or orphans a project.",
            "code guard at begin_member_removal",
        ),
        Rule(
            "complete_after_quiescence",
            "Removal completes only once none of the member's tasks or episodes is live.",
            "code guard at complete_member_removal",
        ),
    ),
)

ENROLLMENT_CODES = Entry(
    name="enrollment codes",
    status=TeamInvitationStatus,
    tables=frozenset({"team_invitations", "team_bootstrap_codes", "owner_sign_in_codes"}),
    writers=(
        SpaceStoreMixin.create_team_invitation,
        SpaceStoreMixin.revoke_team_invitation,
        SpaceStoreMixin.enroll_team_member,
        SpaceStoreMixin.begin_member_removal,
        SpaceStoreMixin.detach_space_authentication_for_restore,
        AppStoreBase._open_current_storage,
        AppStoreBase._migrate_legacy_startup_schema,
        AppStoreBase.online_snapshot,
        OwnerAuthStoreMixin.create_owner_sign_in_code,
        OwnerAuthStoreMixin.redeem_owner_sign_in_code,
    ),
    rules=(
        Rule(
            "bad_guesses_lock_one_code",
            "Repeated bad guesses lock that code only.",
            "projects spec: Team initialization and enrollment",
        ),
        Rule(
            "single_use",
            "A consumed, revoked, locked or expired code never enrolls or signs in.",
            "code guard at enroll_team_member",
        ),
        Rule(
            "creator_revokes_live_invitation",
            "Only a live invitation's creator may revoke it, idempotently keeping the first time.",
            "projects spec: Team initialization and enrollment",
        ),
    ),
)

DEVICE_PAIRING = Entry(
    name="device pairing",
    status=TeamDevicePairingStatus,
    tables=frozenset({"team_device_pairings", "notification_phone_pairings"}),
    writers=(
        SpaceStoreMixin.create_team_device_pairing,
        SpaceStoreMixin.pair_team_device,
        SpaceStoreMixin._withdraw_orphaned_device_pairing,
        SpaceStoreMixin.detach_space_authentication_for_restore,
        NotificationStoreMixin.create_notification_phone_pairing,
        NotificationStoreMixin.redeem_notification_phone_pairing,
        RestoreDetachmentStoreMixin.detach_restored_lifecycle,
    ),
    rules=(
        Rule(
            "single_use_short_lived",
            "A pairing code is short-lived and single-use and locks after repeated guesses.",
            "projects spec: Team initialization and enrollment",
        ),
        Rule(
            "orphan_withdrawn",
            "A pairing whose issuing session is gone is withdrawn before it can pair.",
            "code guard at _withdraw_orphaned_device_pairing",
        ),
        Rule(
            "restore_revokes",
            "Restore revokes pairing codes.",
            "projects spec: Team initialization and enrollment",
        ),
    ),
)

PROJECT_INVITATION = Entry(
    name="project invitation",
    status="response",
    tables=frozenset({"project_invitations", "project_members"}),
    writers=(
        SpaceStoreMixin.invite_to_project,
        SpaceStoreMixin.answer_project_invitation,
        SpaceStoreMixin.seat_project_member,
        SpaceStoreMixin.leave_project,
        SpaceStoreMixin.begin_member_removal,
        SpaceStoreMixin.enroll_team_member,
        AppStoreBase._backfill_project_members,
        ProjectProvisioningStoreMixin.activate_target_project_transfer,
        ProjectStoreMixin.delete_project_records,
        ProjectStoreMixin.migrate_legacy_project_data,
    ),
    rules=(
        Rule(
            "answered_once",
            "An invitation is answered once, and only acceptance seats the member.",
            "code guard at answer_project_invitation",
        ),
        Rule(
            "no_credential",
            "A project invitation carries no credential and cannot enroll its recipient.",
            "projects spec: Project membership and invitations",
        ),
        Rule(
            "last_member_stays",
            "A member may leave only while another member remains.",
            "projects spec: Project membership and invitations",
        ),
    ),
)

AUTO_RESEARCH_RECOVERY = Entry(
    name="Auto-research recovery",
    status=AutoResearchRecoveryStatus,
    tables=frozenset({"auto_research_recoveries"}),
    writers=(
        AutoResearchStoreMixin.schedule_auto_research_task_recovery,
        AutoResearchStoreMixin.complete_auto_research_recovery,
        AutoResearchStoreMixin.defer_auto_research_recovery,
        AutoResearchStoreMixin.release_provider_auth_recoveries,
        AutoResearchStoreMixin.release_settled_auto_research_recovery,
        AutoResearchStoreMixin.restore_settled_auto_research_recovery,
        AutoResearchStoreMixin._abandon_auto_research_recovery_in_connection,
        AutoResearchStoreMixin.detach_auto_research_for_restore,
        migrate_legacy_auto_research,
        ProjectTransferStoreMixin._insert_auto_research_history,
    ),
    rules=(
        Rule(
            "admit_within_attempts",
            "A pending recovery is admitted only while its attempts stay below its maximum.",
            "code guard at complete_auto_research_recovery",
        ),
        Rule(
            "defer_exhausts",
            "A deferred recovery that reaches its maximum attempts becomes exhausted.",
            "code guard at defer_auto_research_recovery",
        ),
        Rule(
            "stop_abandons_recovery",
            "Stop abandons pending recovery rather than launching it.",
            "conversations spec: Graceful Stop and recovery",
        ),
    ),
)

AUTO_RESEARCH_CHILDREN = Entry(
    name="Auto-research children",
    status=AutoResearchChildExperimentState,
    tables=frozenset(
        {
            "auto_research_child_admissions",
            "auto_research_child_experiments",
            "auto_research_child_work",
            "auto_research_lifecycle_notices",
            "auto_research_messages",
        }
    ),
    writers=(
        AutoResearchChildrenStoreMixin._insert_auto_research_child_admission,
        AutoResearchChildrenStoreMixin._reflect_auto_research_child_admission,
        AutoResearchChildrenStoreMixin.cancel_auto_research_child_admission,
        AutoResearchChildrenStoreMixin._activate_auto_research_child_experiment,
        AutoResearchChildrenStoreMixin._settle_auto_research_child_experiment,
        AutoResearchChildrenStoreMixin._terminalize_auto_research_child_experiment_with_notice,
        AutoResearchChildrenStoreMixin.create_auto_research_child_work,
        AutoResearchChildrenStoreMixin.create_auto_research_child_work_recovery,
        AutoResearchChildrenStoreMixin._create_auto_research_child_work_wake_task,
        AutoResearchChildrenStoreMixin.request_auto_research_child_work_stop,
        AutoResearchChildrenStoreMixin._insert_auto_research_lifecycle_notice,
        AutoResearchChildrenStoreMixin._claim_auto_research_lifecycle_notices,
        AutoResearchChildrenStoreMixin._acknowledge_lifecycle_rows,
        AutoResearchChildrenStoreMixin.process_auto_research_lifecycle_inbox,
        AutoResearchChildrenStoreMixin.guard_auto_research_finish,
        AutoResearchChildrenStoreMixin.detach_auto_research_children_for_restore,
        AutoResearchStoreMixin.create_auto_research_continuation,
        AutoResearchStoreMixin._claim_auto_research_root_inputs,
        AutoResearchStoreMixin._insert_auto_research_message,
        AutoResearchStoreMixin.create_auto_research_message_wake_task,
        AutoResearchStoreMixin.mark_auto_research_messages_delivered,
        AutoResearchStoreMixin.readdress_auto_research_question_answer,
        AgentTaskStoreMixin.create_artifact_edit_task,
        AppStoreBase._migrate_per_target_experiment_loops,
        AppStoreBase._migrate_legacy_startup_schema,
        migrate_legacy_auto_research,
        ProjectTransferStoreMixin._insert_auto_research_history,
        ProjectStoreMixin.delete_project_records,
        ProjectStoreMixin.migrate_legacy_project_data,
    ),
    rules=(
        Rule(
            "finish_waits_for_blockers",
            "Finish is refused while any child blocker remains.",
            "auto-research spec: Completion, Stop, and report",
        ),
        Rule(
            "claim_skips_suppressed",
            "Claiming delivers only pending notices whose wake is not suppressed.",
            "code guard at _claim_auto_research_lifecycle_notices",
        ),
        Rule(
            "notices_move_forward",
            "A lifecycle notice moves from pending to delivered to acknowledged and never back.",
            "auto-research spec: Mail and lifecycle notices",
        ),
    ),
)

EPISODE_ISOLATION = Entry(
    name="episode isolation and merge",
    status=_field(EpisodeIsolationState, "status"),
    tables=frozenset({"episode_isolations", "episode_isolation_states"}),
    writers=(
        ConversationWorktreeStoreMixin.create_episode_isolation,
        ConversationWorktreeStoreMixin.set_episode_isolation_status,
        ConversationWorktreeStoreMixin._write_episode_isolation_state,
        ConversationWorktreeStoreMixin.reserve_episode_merge,
        ConversationWorktreeStoreMixin.set_branch_archived,
        ensure_episode_worktree,
        merge_episode,
        complete_graph_merge,
        reconcile_episode_merge,
        cleanup_episode,
        ProjectTransferStoreMixin._insert_transfer_episodes,
        ProjectStoreMixin.delete_project_records,
    ),
    rules=(
        Rule(
            "merge_reserves_first",
            "Merge reserves the owner and persists an attempt before committing leftovers.",
            "auto-research spec: Episode code merge and cleanup",
        ),
        Rule(
            "live_turns_block_removal",
            "Isolation is removed only while its binding is quiescent.",
            "code guard at require_episode_binding_quiescent",
        ),
        Rule(
            "branch_deletion_after_delivery",
            "The worktree branch is deleted only after the worktree is removed and its "
            "delivery verified.",
            "auto-research spec: Episode code merge and cleanup",
        ),
    ),
)

CONVERSATION_WORKTREE = Entry(
    name="conversation worktree",
    status=_field(ConversationWorktreeBinding, "status"),
    tables=frozenset({"conversation_worktrees"}),
    writers=(
        ConversationWorktreeStoreMixin.create_conversation_worktree,
        ConversationWorktreeStoreMixin.set_conversation_worktree_status,
        admit_conversation_worktree,
        remove_conversation_worktree,
        ProjectStoreMixin.delete_project_records,
        ProjectStoreMixin.migrate_legacy_project_data,
        ProjectStoreMixin.migrate_project_identity,
    ),
    rules=(
        Rule(
            "binding_before_creation",
            "RCP persists the immutable binding before it creates the worktree.",
            "conversations spec: Conversation worktrees",
        ),
        Rule(
            "binding_fixed_after_work",
            "The binding cannot change once the chat has a Work turn.",
            "conversations spec: Conversation worktrees",
        ),
    ),
)

BRANCH_MERGE = Entry(
    name="branch merge",
    status=_field(GraphBranchSummary, "merge_state"),
    tables=frozenset({"graph_branches"}),
    writers=(
        start_branch_merge,
        start_human_branch_merge,
        start_owned_branch_merge,
        settle_agentless_merge,
        recover_branch_merge_task,
        stream_branch_merge_run,
        commit_branch_merge_with_history,
        BranchNameStoreMixin.rename_branch,
        _insert_graph_branch,
        backfill_graph_branches,
        AutoResearchChildrenStoreMixin.archive_empty_auto_research_fork,
        ConversationWorktreeStoreMixin._write_episode_isolation_state,
        ConversationWorktreeStoreMixin.set_branch_archived,
        ProjectTransferStoreMixin._insert_transfer_episodes,
        ProjectStoreMixin.delete_project_records,
    ),
    rules=(
        Rule(
            "delivery_needs_newer_head",
            "Graph delivery needs a head newer than the base with no successful receipt for "
            "that head.",
            "auto-research spec: Branch lifecycle and merge eligibility",
        ),
        Rule(
            "no_live_graph_writer",
            "Graph delivery needs no queued, running or pausing graph writer on the branch.",
            "auto-research spec: Branch lifecycle and merge eligibility",
        ),
        Rule(
            "agents_never_merge",
            "Only a human dispatches a branch merge, except the Auto-research orchestrator "
            "on the forks its lineage owns.",
            "invariant 3",
        ),
    ),
)

COMPUTE_JOB = Entry(
    name="compute job",
    status=ComputeJobStatus,
    tables=frozenset({"compute_jobs"}),
    writers=(
        ComputeJobStoreMixin.create_compute_job,
        ComputeJobStoreMixin.record_compute_job_refresh,
        ComputeJobStoreMixin.record_compute_job_cancelled,
        ComputeJobStoreMixin.record_compute_job_exited,
        ComputeJobStoreMixin._record_compute_job_completion,
        launch_compute_job,
        refresh_compute_job,
        reconcile_compute_jobs,
        _rebind_local_stage_paths,
        ProjectStoreMixin.delete_project_records,
        ProjectStoreMixin.migrate_legacy_project_data,
    ),
    rules=(
        Rule(
            "complete_from_running_or_lost",
            "A job completes only from running or lost.",
            "code guard at _record_compute_job_completion",
        ),
        Rule(
            "confirmed_absence_settles_cancel",
            "A cancel settles only on a fresh check that confirms the job stopped, and is "
            "never replayed automatically.",
            "machines spec: One watcher and human Cancel contract",
        ),
    ),
)

PAPER_CHANGES = Entry(
    name="paper changes",
    status=_field(ChangeGroup, "state"),
    tables=frozenset({"paper_change_groups", "paper_changes", "paper_change_receipts"}),
    writers=(
        PaperChanges.collect,
        PaperChanges._state,
        PaperChanges._apply_prepared,
        PaperChanges._save_receipt,
        PaperChanges.accept,
        PaperChanges.reject,
        PaperChanges.undo,
        PaperChanges.accept_all,
        PaperChanges.undo_all,
        PaperLive._apply,
        restore_paper_section,
    ),
    rules=(
        Rule(
            "decide_from_open",
            "A group is accepted or rejected only from pending or outdated.",
            "code guard at PaperChanges.accept",
        ),
        Rule(
            "undo_from_accepted",
            "Undo applies only to an accepted or applied group.",
            "code guard at PaperChanges.undo",
        ),
        Rule(
            "coach_writes_changes_only",
            "The paper coach writes only its own changes folder, which RCP turns into "
            "change cards.",
            "invariant 4",
        ),
    ),
)

PAPER_LIVE = Entry(
    name="paper live",
    status=_field(PaperTree, "publish_state"),
    tables=frozenset({"paper_publication"}),
    writers=(
        PaperLive._initialize,
        PaperLive._revision,
        PaperLive._publication_result,
        PaperLive.publish,
        PaperLive.resolve_hold,
        PaperLive.pause,
        PaperLive.resume,
        PaperLive.drain,
        PaperLive.dispose,
        PaperLive.fence,
        BackupCaptureCoordinator.capture_sqlite,
        restore_paper_section,
    ),
    rules=(
        Rule(
            "commit_before_broadcast",
            "An edit commits in SQLite before it is broadcast.",
            "paper spec: Shared paper",
        ),
        Rule(
            "paused_refuses_writes",
            "A paused or fenced project refuses paper writes.",
            "code guard at PaperLive.run_sync",
        ),
        Rule(
            "published_revision_rises",
            "The published revision only rises.",
            "code guard at PaperLive._publication_result",
        ),
        Rule(
            "hold_blocks_publish",
            "Nothing publishes while a hold is set.",
            "code guard at PaperLive._publish",
        ),
    ),
)

MIRRORED_TERMINAL = Entry(
    name="mirrored terminal",
    status="ended_at",
    tables=frozenset(),
    writers=(
        TerminalManager.open,
        TerminalManager.attach,
        TerminalManager.detach,
        TerminalManager.write,
        TerminalManager.sweep,
        TerminalManager.end,
        TerminalManager.end_all,
        TerminalManager.reconcile_registrations,
        TerminalManager._reconcile_record,
        TerminalManager._finish_retained,
        TerminalManager.mark_unresolved,
        TerminalManager.unit_confirmed_gone,
    ),
    rules=(
        Rule(
            "one_shell_per_tree",
            "One shell runs per resolved working tree across aliases and projects.",
            "projects spec: Member terminal lifecycle",
        ),
        Rule(
            "finish_after_unit_gone",
            "A mirrored record finishes only after its unit is confirmed gone.",
            "projects spec: Member terminal lifecycle",
        ),
        Rule(
            "uncertain_stop_blocks",
            "An uncertain stop or unreadable record blocks a second shell on that tree.",
            "projects spec: Member terminal lifecycle",
        ),
    ),
)

ARTIFACT = Entry(
    name="artifact",
    status="state",
    tables=frozenset({"artifacts", "artifact_versions", "artifact_imports"}),
    writers=(
        insert_artifact,
        ArtifactStoreMixin.create_artifact,
        ArtifactStoreMixin.keep_artifact,
        ArtifactStoreMixin._retain_artifact,
        ArtifactStoreMixin.publish_artifact_version,
        ArtifactStoreMixin._save_artifact_version_metadata,
        ArtifactStoreMixin.record_artifact_sync,
        ArtifactStoreMixin.undo_artifact,
        ArtifactStoreMixin.delete_artifact,
        ArtifactStoreMixin.expire_artifacts,
        ArtifactStoreMixin._remove_artifact,
        ArtifactImportStoreMixin.record_artifact_import_failure,
        ArtifactImportStoreMixin.complete_artifact_import,
        ConsolidationStoreMixin.resolve_consolidation_run,
        ConsolidationStoreMixin.settle_consolidation_run,
        AppStoreBase._migrate_artifact_live_policy,
        ProjectTransferStoreMixin._insert_transfer_episodes,
        ProjectStoreMixin.delete_project_records,
        ProjectStoreMixin.migrate_legacy_project_data,
    ),
    rules=(
        Rule(
            "holds_refuse_delete",
            "Queued, running or pausing readers and editors refuse deletion.",
            "paper spec: Deleting an artifact",
        ),
        Rule(
            "tombstone_stops_reimport",
            "Delete and expiry leave a tombstone, and a deleted identity never recreates.",
            "paper spec: Deleting an artifact",
        ),
        Rule(
            "one_lock",
            "Publication, Undo, Keep and expiry run under the artifact's one lock.",
            "paper spec: Keeping an artifact",
        ),
    ),
)

SERVER_STEP_JOURNAL = Entry(
    name="server step journal",
    status=ServerStepState,
    tables=frozenset(),
    writers=(
        ServerEventEmitter.emit_step,
        ServerEventEmitter.fail_unexpected,
        ServerEventEmitter.finish,
    ),
    rules=(
        Rule(
            "plan_order",
            "Step events follow plan order.",
            "code guard at _validate_event_sequence",
        ),
        Rule(
            "earlier_steps_succeed_first",
            "No step begins before every earlier step succeeded.",
            "code guard at _validate_event_sequence",
        ),
        Rule(
            "nothing_after_failure",
            "No step event follows a terminal failure or pause.",
            "code guard at _validate_event_sequence",
        ),
    ),
)

RESTORE = Entry(
    name="restore",
    status=_field(RestoreRepositoryRecovery, "state"),
    tables=frozenset(),
    writers=(
        prepare_restore,
        detach_restore_database,
        _recover_repositories,
        RestoreDetachmentStoreMixin.detach_restored_lifecycle,
        ProjectProvisioningStoreMixin.detach_project_provisioning_for_restore,
        ProjectProvisioningStoreMixin.detach_project_transfers_for_restore,
        ProviderLoginStoreMixin.detach_provider_logins_for_restore,
        SpaceStoreMixin.detach_space_authentication_for_restore,
        AutoResearchStoreMixin.detach_auto_research_for_restore,
        AutoResearchChildrenStoreMixin.detach_auto_research_children_for_restore,
        ChatForkStoreMixin.detach_chat_forks_for_restore,
        ProjectStoreMixin.rebind_project_registration_for_restore,
        ProjectStoreMixin.complete_project_publication_for_restore,
        ProjectStoreMixin.mark_uncaptured_project_unavailable_for_restore,
    ),
    rules=(
        Rule(
            "old_authority_cannot_resume",
            "Restore needs operator confirmation that old authority cannot resume, and no old "
            "turn, watcher, retry or graph mutation auto-launches.",
            "server spec: Backup and restore",
        ),
        Rule(
            "checkouts_before_publication",
            "Every captured checkout is reconstructed before research publication.",
            "server spec: Backup and restore",
        ),
    ),
)

PROJECT_DELETION = Entry(
    name="project deletion",
    status="retired_at",
    tables=frozenset({"projects", "memory_deletions", "browser_owners"}),
    writers=(
        ProjectCatalog.delete,
        stop_project_work,
        _cancel_external_work,
        ProjectStoreMixin.upsert_project,
        ProjectStoreMixin.update_project_summary,
        ProjectStoreMixin.delete_project_records,
        ProjectStoreMixin.migrate_project_identity,
        ProjectStoreMixin.rebind_project_registration_for_restore,
        ProjectStoreMixin.complete_project_publication_for_restore,
        ProjectStoreMixin.mark_uncaptured_project_unavailable_for_restore,
        ProjectProvisioningStoreMixin._insert_target_activation_project,
        ProjectProvisioningStoreMixin.retire_source_project_transfer,
        ProjectGroupStoreMixin.set_project_group,
        ProjectGroupStoreMixin.delete_project_group,
        ChatDisplayStoreMixin.record_browser_owner,
        ChatDisplayStoreMixin.set_chat_browser_requested,
        RestoreDetachmentStoreMixin.detach_restored_lifecycle,
        close_chat_browser_owners,
        retry_browser_cleanup,
        _retry_memory_cleanup,
        _rebind_local_stage_paths,
        build_rehearsal_overlay,
        prepare_restore,
    ),
    rules=(
        Rule(
            "stop_token_matches",
            "Delete stops only work the human was shown and refuses when the work left no "
            "longer matches the preview's stop token.",
            "projects spec: Deletion",
        ),
        Rule(
            "refusals_before_stops",
            "The non-destructive refusals run before anything stops.",
            "projects spec: Deletion",
        ),
        Rule(
            "fence_admits_no_work",
            "The deletion fence refuses new work for the project.",
            "projects spec: Deletion",
        ),
    ),
)

EVIDENCE_FLAG = Entry(
    name="evidence flag",
    status="state",
    tables=frozenset({"evidence_flags"}),
    writers=(
        EvidenceFlagStoreMixin.execute_evidence_flag_command,
        EvidenceFlagStoreMixin.dismiss_evidence_flag,
        EvidenceFlagStoreMixin.carry_merge_evidence_flags,
        record_evidence_flag_address,
        settle_evidence_flag_address,
        settle_ended_evidence_flag_addresses,
        adopt_legacy_evidence_flag_requests,
    ),
    rules=(
        Rule(
            "one_live_flag",
            "Only one open or addressing flag exists per project, target, Evidence and kind.",
            "consolidation spec: Evidence to check",
        ),
        Rule(
            "address_settles",
            "An addressing flag returns to open, or is dismissed when its address run "
            "succeeded and resolved it.",
            "code guard at settle_evidence_flag_address",
        ),
        Rule(
            "carry_only_unchanged",
            "A merge carries a finding only when its Evidence is current and unchanged.",
            "consolidation spec: Evidence to check",
        ),
    ),
)

NOTIFICATION_DELIVERY = Entry(
    name="notification delivery",
    status="last_status",
    tables=frozenset({"notification_outbox", "notification_devices"}),
    writers=(
        NotificationStoreMixin.enqueue_notification,
        NotificationStoreMixin.begin_notification_attempt,
        NotificationStoreMixin.defer_notification,
        NotificationStoreMixin.drop_notification,
        NotificationStoreMixin.fail_notification,
        NotificationStoreMixin.acknowledge_notification,
        NotificationStoreMixin.expire_notification_receipts,
        NotificationStoreMixin._guard_notification_delivery,
        NotificationStoreMixin._rewrite_notification_project_links,
        NotificationStoreMixin.register_notification_device,
        NotificationStoreMixin.register_web_push_device,
        NotificationStoreMixin._insert_web_push_device,
        NotificationStoreMixin._notification_device_active,
        NotificationStoreMixin.set_notification_device_status,
        NotificationStoreMixin.delete_notification_device,
        RestoreDetachmentStoreMixin.detach_restored_lifecycle,
        ProjectProvisioningStoreMixin.retire_source_project_transfer,
    ),
    rules=(
        Rule(
            "membership_rechecked",
            "Before every delivery or retry, current membership and preference must still "
            "permit the item, or its queued row is dropped.",
            "projects spec: Project membership and invitations",
        ),
        Rule(
            "expired_session_deletes_device",
            "An expired owning session deletes the device and its queued rows.",
            "projects spec: Project membership and invitations",
        ),
        Rule(
            "restore_detaches_devices",
            "Restore detaches notification devices and resets observations to a quiet baseline.",
            "server spec: Backup and restore",
        ),
    ),
)

ENTRIES = (
    PROJECT_PROVISIONING,
    PROJECT_TRANSFER,
    PROVIDER_LOGIN,
    MEMBER_REMOVAL,
    ENROLLMENT_CODES,
    DEVICE_PAIRING,
    PROJECT_INVITATION,
    AUTO_RESEARCH_RECOVERY,
    AUTO_RESEARCH_CHILDREN,
    EPISODE_ISOLATION,
    CONVERSATION_WORKTREE,
    BRANCH_MERGE,
    COMPUTE_JOB,
    PAPER_CHANGES,
    PAPER_LIVE,
    MIRRORED_TERMINAL,
    ARTIFACT,
    SERVER_STEP_JOURNAL,
    RESTORE,
    PROJECT_DELETION,
    EVIDENCE_FLAG,
    NOTIFICATION_DELIVERY,
)

NOT_LIFECYCLE: dict[str, str] = {
    "_legacy_campaigns_archive": "frozen archive of retired campaign rows, only deleted",
    "agent_threads": "thread identity row; closing is one-way",
    "agent_usage": "append-only usage events",
    "artifact_operations": "index from an operation to the artifact it published",
    "artifact_revision_candidates": "frozen legacy rows read only for byte import",
    "auto_research_apply_results": "append-only Apply receipts",
    "auto_research_child_work_attempts": "append-only allocation ledger for worker turns",
    "auto_research_command_files": "append-only staged command files",
    "auto_research_episodes": "identity row for an Auto-research episode",
    "auto_research_experiment_invocations": "append-only allowance ledger",
    "auto_research_finish_receipts": "append-only Finish receipts",
    "auto_research_inbox_receipts": "append-only harvest and clear receipts",
    "auto_research_invocations": "invocation ledger; handoffs_cleared_at is a one-shot mark",
    "branch_name_reservations": "branch name index",
    "branch_names": "branch name index",
    "browser_turn_status": "per-turn browser display cache",
    "bulletin_command_receipts": "keyed command receipts",
    "bulletin_mention_observations": "notification observation cursor",
    "bulletin_posts": "message rows; edit and delete are marks, not states",
    "bulletin_reads": "per-member read cursor",
    "chat_browser_preferences": "per-chat preference",
    "chat_display": "per-chat title and archive preference",
    "chat_forks": "fork identity record",
    "chat_pins": "per-member preference",
    "chat_reads": "per-member read cursor",
    "chat_session_contexts": "native session context cache",
    "client_requests": "idempotency receipts",
    "compute_backend_probes": "probe result cache",
    "digest_events": "append-only digest events",
    "digest_heads": "projector cursor",
    "digest_marks": "per-member read cursor",
    "episode_archives": "per-episode archive preference",
    "episode_invocations": "append-only invocation ledger",
    "episode_merge_request_receipts": "idempotency receipts for merge requests",
    "evidence_flag_carries": "append-only carry receipts",
    "evidence_flag_command_receipts": "keyed command receipts",
    "graph_run_contracts": "append-only launch contracts",
    "graph_run_events": "append-only task events",
    "graph_run_outputs": "task Patch output, pruned by age",
    "graph_seen_heads": "per-session graph head cursor",
    "graph_watcher_reconciliation": "watcher boundary cursor",
    "memory_migrations": "one-shot migration receipts",
    "notification_episode_observations": "observation cursor",
    "notification_graph_markers": "observation cursor",
    "notification_preferences": "per-member preference",
    "notification_project_baselines": "observation baseline",
    "notification_question_events": "observation cursor",
    "notification_vapid_key": "singleton key material",
    "notification_web_push_subscriptions": "device subscription identity",
    "owner_credentials": "singleton owner secret hash",
    "paper_blobs": "content-addressed bodies",
    "paper_doc_states": "compacted document cache",
    "paper_drafts": "legacy draft content",
    "paper_files": "file tree rows; deleted_at is a tombstone",
    "paper_revisions": "append-only revision log, pruned",
    "paper_saved_snapshots": "saved manifests per revision",
    "paper_updates": "append-only edit log, compacted",
    "paper_version_texts": "content-addressed bodies",
    "paper_versions": "named version manifests",
    "project_aliases": "identity alias map",
    "project_groups": "group identity rows",
    "provider_login_settings": "per-member preference",
    "provider_readiness_snapshots": "probe result cache",
    "provider_skill_inventories": "skill listing cache; status is the last refresh outcome",
    "research_view_pins": "per-member preference",
    "resource_deadlines": "resource expiry index",
    "resource_versions": "change counters",
    "resource_watermark_epoch": "singleton counter",
    "space_identity": "singleton space identity",
    "space_machines": "machine configuration rows",
    "space_turn_project_bindings": "per-turn binding receipt",
    "storage_schema_migrations": "migration receipts",
    "team_member_tokens": "credential identity rows",
    "team_sessions": "session identity rows",
    "writing_sessions": "native session cache",
}

UNKNOWN_TABLE: dict[str, str] = {
    "rcp.paper.persistence:collect_bodies": "deletes unreferenced paper_version_texts and paper_blobs",
    "rcp.paper.persistence:prune": "prunes paper_updates and paper_doc_states of deleted files",
    "rcp.runs.tasks.work:record_work_graph_failure": "prose that reads as SQL, not a write",
    "rcp.server_ops.provider_update:_pending_steps": "prose that reads as SQL, not a write",
    "rcp.storage.base:AppStoreBase._rebuild_storage_table": (
        "schema migration copies a table's rows into its rebuilt self"
    ),
    "rcp.storage.base:AppStoreBase._replace_lineage_column": (
        "schema migration folds a legacy lineage column into episode_id"
    ),
    "rcp.storage.base:AppStoreBase._rewrite_lineage_json_column": (
        "schema migration rewrites legacy lineage keys inside JSON columns"
    ),
}
