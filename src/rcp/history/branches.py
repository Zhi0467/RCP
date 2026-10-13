from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rcp.core.authority import refused_authorizer
from rcp.core.materialize import (
    AcceptedPatchObserver,
    MaterializationResult,
)
from rcp.core.models import (
    AuthorizedHuman,
    BranchMergeProvenance,
    BranchMergeReceipt,
    GraphBranchMetadata,
    GraphState,
    Patch,
)
from rcp.core.operations import SetProjectTruthScopeOperation
from rcp.core.research_md import render_research_md
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef, TransitionTrace
from rcp.core.transitions import (
    PreparedTransition,
    project_transition_projection,
)
from rcp.core.validation import ValidationReport
from rcp.history.delta import build_revision_summaries
from rcp.limits import REMOTE_STATE_RECONCILE_WINDOW_SECONDS
from rcp.transport import BatchPublishFailed, StateUnavailable

if TYPE_CHECKING:
    from rcp.history.engine import ProjectHistoryEngine
    from rcp.history.manager import HistoryManager


_PATCH_NAME = re.compile(r"[0-9]{6}\.json")
_MERGE_NAME = re.compile(r"[a-f0-9]{64}\.json")


@dataclass(frozen=True)
class BranchReadSnapshot:
    """Read-only branch projection from one coherent canonical-state snapshot."""

    metadata: GraphBranchMetadata
    receipts: tuple[BranchMergeReceipt, ...]
    state: GraphState


def canonical_branch_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError("graph branch id must be a canonical episode UUIDv4") from exc
    if str(parsed) != value or parsed.version != 4:
        raise ValueError("graph branch id must be a canonical episode UUIDv4")
    return value


class BranchHistoryManager:
    """One episode-owned graph history layered on an immutable main prefix."""

    def __init__(self, parent: HistoryManager, metadata: GraphBranchMetadata) -> None:
        self.parent = parent
        self.manifest = parent.manifest
        self.workspace = parent.workspace
        self.root = _safe_branch_root(parent.root, metadata.branch_id, create=False)
        self.patches_dir = self.root / "patches"
        self.merges_dir = self.root / "merges"
        self._process_lock = parent._process_lock
        self._metadata = metadata
        self._identity_metadata = metadata

    @property
    def history_engine(self) -> ProjectHistoryEngine:
        return self.parent.history_engine

    @property
    def graph_target(self) -> GraphTargetRef:
        return GraphTargetRef(kind="branch", branch_id=self._metadata.branch_id)

    @property
    def branch_id(self) -> str:
        return self._metadata.branch_id

    def initialize(self) -> MaterializationResult:
        with self.workspace.transaction(), self.parent._append_lock():
            self.parent._reload_manifest()
            self.manifest = self.parent.manifest
            main = self.parent.materialize(write_outputs=False)
            self.parent.require_writable(main.state)
            self.parent._require_writable_home_locked(main)
            self._metadata = self._read_metadata()
            result = self.materialize(write_outputs=False)
            self.require_writable(result.state)
            self.ensure_layout()
            expected_metadata = self._metadata.model_copy(update={"head": self.head_ref(result)})
            metadata_changed = expected_metadata != self._metadata
            needs_repair = metadata_changed or not self._outputs_coherent(result)
            self._metadata = expected_metadata
            if needs_repair:
                if metadata_changed:
                    self._write_metadata(expected_metadata)
                self._write_materialized_outputs(result)
                self.workspace.publish(self._published_paths(include_metadata=metadata_changed))
            return result

    def ensure_layout(self) -> None:
        _safe_branch_root(self.parent.root, self.branch_id, create=True)
        for path in (self.patches_dir, self.merges_dir):
            if os.path.lexists(path):
                if not stat.S_ISDIR(path.lstat().st_mode):
                    raise ValueError(f"graph branch path is not a regular directory: {path}")
            else:
                path.mkdir(mode=0o700)

    def branch_metadata(self) -> GraphBranchMetadata:
        generation = self.history_engine.snapshot(self.graph_target, refresh=False)
        metadata = generation.source.metadata
        assert metadata is not None
        self._validate_metadata(metadata)
        return metadata.model_copy(
            update={"head": generation.head(generation.result.state.revision)}
        )

    def head_ref(self, materialization: MaterializationResult | None = None) -> GraphHeadRef:
        result = materialization or self.current_materialization()
        transition_id = self._metadata.base_head.transition_id
        for patch in result.patches:
            if patch.revision <= self._metadata.base_head.revision:
                continue
            if patch.admission == "accepted" and patch.transition is not None:
                transition_id = patch.transition.transition_id
        return GraphHeadRef(
            target=self.graph_target,
            revision=result.state.revision,
            transition_id=transition_id,
        )

    def current_accepted_revision(self) -> int:
        result = self.current_materialization()
        return max(
            (
                revision
                for revision, report in result.reports.items()
                if revision > self._metadata.base_head.revision and not report.rejected
            ),
            default=self._metadata.base_head.revision,
        )

    def state(self) -> GraphState:
        return self.current_materialization().state

    def base_state(self) -> GraphState:
        """Replay and return the immutable accepted main prefix for this branch."""

        with self._process_lock:
            if not self.workspace.refresh_if_stale():
                raise StateUnavailable("canonical state refresh did not confirm a current snapshot")
            self.parent._reload_manifest()
            self.manifest = self.parent.manifest
            self._metadata = self._read_metadata()
            generation = self.history_engine.snapshot(
                self.graph_target, refresh=False, use_store=False
            )
            return generation.initial

    def current_materialization(
        self,
        *,
        refresh_max_age_seconds: float = REMOTE_STATE_RECONCILE_WINDOW_SECONDS,
    ) -> MaterializationResult:
        with self._process_lock:
            with suppress(StateUnavailable):
                self.workspace.refresh_if_stale(refresh_max_age_seconds)
            self.parent._reload_manifest()
            self.manifest = self.parent.manifest
            self._metadata = self._read_metadata()
            return self.materialize(write_outputs=False)

    def require_writable(self, state: GraphState | None = None) -> GraphState:
        from rcp.history.manager import ReplayHalted

        current = state or self.current_materialization().state
        if current.replay_status == "degraded":
            raise ReplayHalted(current)
        return current

    def load_patches(self) -> list[Patch]:
        with self._process_lock:
            return [
                self.parent._decode_persisted_patch(path.read_text(encoding="utf-8"))
                for path in self._patch_paths()
            ]

    def append(
        self,
        patch: Patch,
        *,
        raise_on_reject: bool = True,
        discard_on_reject: bool = False,
        expected_revision: int | None = None,
        authorized_by: AuthorizedHuman | None = None,
    ) -> tuple[Patch, MaterializationResult]:
        with self.workspace.transaction(), self.parent._append_lock():
            return self._append_locked(
                patch,
                raise_on_reject=raise_on_reject,
                discard_on_reject=discard_on_reject,
                expected_revision=expected_revision,
                authorized_by=authorized_by,
            )

    def _append_locked(
        self,
        patch: Patch,
        *,
        raise_on_reject: bool,
        discard_on_reject: bool,
        expected_revision: int | None,
        authorized_by: AuthorizedHuman | None,
    ) -> tuple[Patch, MaterializationResult]:
        from rcp.history.manager import PatchRejected, RevisionConflict

        self.parent._reload_manifest()
        self.manifest = self.parent.manifest
        self._metadata = self._read_metadata()
        current = self.materialize(write_outputs=False)
        self.require_writable(current.state)
        self.ensure_layout()
        current = self._repair_materializations_locked(current)
        if expected_revision is not None and current.state.revision != expected_revision:
            raise RevisionConflict(
                f"the branch graph moved from revision {expected_revision} to "
                f"{current.state.revision} while this patch was being written"
            )
        self._require_branch_patch(patch)
        revision = self._next_revision()
        prepared, report, _candidate = self.parent._prepare_transition_locked(
            current,
            [patch],
            revision,
            authorized_by=authorized_by,
            pre_head=self.head_ref(current),
            apply_target=self.graph_target,
        )
        self._require_prepared_branch_patch(prepared)
        if report.rejected and (discard_on_reject or refused_authorizer(report)):
            raise PatchRejected(report)
        prepared = prepared.model_copy(
            update={
                "admission": "rejected" if report.rejected else "accepted",
                "admission_messages": list(report.messages),
            }
        )
        target = self.patches_dir / f"{revision:06d}.json"
        payload = prepared.model_dump_json(indent=2) + "\n"
        if os.path.lexists(target):
            _require_regular_file(target, "branch patch")
            if target.read_text(encoding="utf-8") != payload:
                raise ValueError(f"branch patch revision {revision} already has different content")
        else:
            self.parent._atomic_text(target, payload)
        try:
            result = self.materialize(write_outputs=False)
            self._write_committed_materializations(result)
        except Exception:
            self.parent._require_branch_materialization_repair(self.branch_id)
            raise
        paths = [
            target.relative_to(self.parent.root),
            *self._published_paths(include_metadata=True),
        ]
        try:
            self.workspace.publish_committed_patch(paths, target.relative_to(self.parent.root))
        except Exception as exc:
            if not self.workspace.remote:
                raise
            if not self._reconcile_patch_publish_failure(exc, target):
                raise
        if raise_on_reject and report.rejected:
            raise PatchRejected(report)
        if not report.rejected:
            self._notify_accepted_transition(result.state.revision)
        return prepared, result

    def _notify_accepted_transition(self, revision: int) -> None:
        if self.parent.on_accepted_branch_transition is not None:
            try:
                self.parent.on_accepted_branch_transition(self.graph_target.key, revision)
            except Exception:
                logging.getLogger(__name__).exception("Accepted branch signal failed")

    def append_batch(
        self,
        patches: list[Patch],
        *,
        expected_revision: int | None = None,
        authorized_by: AuthorizedHuman | None = None,
    ) -> tuple[list[Patch], MaterializationResult]:
        if not patches:
            return [], self.current_materialization()
        return self.append_batch_from_state(
            lambda _state: patches,
            expected_revision=expected_revision,
            authorized_by=authorized_by,
        )

    def append_batch_from_state(
        self,
        build_patches: Callable[[GraphState], list[Patch]],
        *,
        expected_revision: int | None = None,
        authorized_by: AuthorizedHuman | None = None,
    ) -> tuple[list[Patch], MaterializationResult]:
        from rcp.history.manager import PatchRejected, RevisionConflict

        with self.workspace.transaction(), self.parent._append_lock():
            self.parent._reload_manifest()
            self.manifest = self.parent.manifest
            self._metadata = self._read_metadata()
            current = self.materialize(write_outputs=False)
            self.require_writable(current.state)
            self.ensure_layout()
            current = self._repair_materializations_locked(current)
            if expected_revision is not None and current.state.revision != expected_revision:
                raise RevisionConflict(
                    "The branch graph changed after this draft began; reload before syncing it."
                )
            patches = build_patches(current.state)
            if not patches:
                return [], current
            for patch in patches:
                self._require_branch_patch(patch)
            revision = self._next_revision()
            prepared, report, _candidate = self.parent._prepare_transition_locked(
                current,
                patches,
                revision,
                authorized_by=authorized_by,
                pre_head=self.head_ref(current),
                apply_target=self.graph_target,
            )
            self._require_prepared_branch_patch(prepared)
            if report.rejected:
                raise PatchRejected(report)
            prepared = prepared.model_copy(
                update={"admission": "accepted", "admission_messages": list(report.messages)}
            )
            target = self.patches_dir / f"{revision:06d}.json"
            payload = prepared.model_dump_json(indent=2) + "\n"
            if os.path.lexists(target):
                _require_regular_file(target, "branch patch")
                if target.read_text(encoding="utf-8") != payload:
                    raise ValueError(
                        f"branch patch revision {revision} already has different content"
                    )
            else:
                self.parent._atomic_text(target, payload)
            try:
                result = self.materialize(write_outputs=False)
                self._write_committed_materializations(result)
            except Exception:
                self.parent._require_branch_materialization_repair(self.branch_id)
                raise
            paths = [
                target.relative_to(self.parent.root),
                *self._published_paths(include_metadata=True),
            ]
            try:
                self.workspace.publish_committed_patch(paths, target.relative_to(self.parent.root))
            except Exception as exc:
                if not self.workspace.remote:
                    raise
                if not self._reconcile_patch_publish_failure(exc, target):
                    raise
            self._notify_accepted_transition(result.state.revision)
            return [prepared], result

    def validate_candidate(
        self,
        patch: Patch,
        *,
        authorized_by: AuthorizedHuman | None = None,
    ) -> tuple[Patch, ValidationReport, GraphState]:
        with self.workspace.transaction(), self.parent._append_lock():
            self._metadata = self._read_metadata()
            current = self.materialize(write_outputs=False)
            self.require_writable(current.state)
            self._require_branch_patch(patch)
            prepared, report, _candidate = self.parent._prepare_transition_locked(
                current,
                [patch],
                self._next_revision(),
                authorized_by=authorized_by,
                pre_head=self.head_ref(current),
                apply_target=self.graph_target,
            )
            if not report.rejected:
                self._require_prepared_branch_patch(prepared)
            return prepared, report, current.state

    def preview_batch_from_state(
        self,
        build_patches: Callable[[GraphState], list[Patch]],
        *,
        expected_revision: int,
        authorized_by: AuthorizedHuman | None = None,
    ) -> PreparedTransition:
        from rcp.history.manager import PatchRejected, RevisionConflict

        with self.workspace.transaction(), self.parent._append_lock():
            self._metadata = self._read_metadata()
            current = self.materialize(write_outputs=False)
            self.require_writable(current.state)
            if current.state.revision != expected_revision:
                raise RevisionConflict(
                    f"the branch graph moved from revision {expected_revision} to "
                    f"{current.state.revision} while this draft was being previewed"
                )
            patches = build_patches(current.state)
            if not patches:
                raise ValueError("the staged draft has no semantic graph change to preview")
            for patch in patches:
                self._require_branch_patch(patch)
            prepared, report, candidate = self.parent._prepare_transition_locked(
                current,
                patches,
                self._next_revision(),
                authorized_by=authorized_by,
                pre_head=self.head_ref(current),
                apply_target=self.graph_target,
            )
            if report.rejected or candidate is None:
                raise PatchRejected(report)
            self._require_prepared_branch_patch(prepared)
            assert prepared.transition is not None
            return PreparedTransition(
                patch=prepared,
                projection=project_transition_projection(
                    candidate,
                    prepared.transition,
                    canonical=False,
                ),
            )

    def materialize(
        self,
        *,
        write_outputs: bool = True,
        accepted_patch_observer: AcceptedPatchObserver | None = None,
    ) -> MaterializationResult:
        with self._process_lock:
            self._metadata = self._read_metadata()
            result = self.history_engine.materialization(
                self.graph_target, refresh=False, accepted_patch_observer=accepted_patch_observer
            )
            if write_outputs and result.state.replay_status == "complete":
                self.ensure_layout()
                self._write_materialized_outputs(result)
            return result

    def transition_events_after(self, revision: int) -> list[dict[str, object]]:
        events: list[dict[str, object]] = []
        for patch in self.current_materialization().patches:
            if (
                patch.revision <= max(revision, self._metadata.base_head.revision)
                or patch.admission != "accepted"
                or patch.transition is None
            ):
                continue
            head = GraphHeadRef(
                target=self.graph_target,
                revision=patch.revision,
                transition_id=patch.transition.transition_id,
            )
            for event in patch.transition.lifecycle_events:
                events.append(
                    {"head": head.model_dump(mode="json"), "event": event.model_dump(mode="json")}
                )
        return events

    def transition_trace_at_revision(self, revision: int) -> TransitionTrace | None:
        if revision <= self._metadata.base_head.revision:
            return None
        path = self.patches_dir / f"{revision:06d}.json"
        if not path.is_file():
            return None
        patch = self.parent._decode_persisted_patch(path.read_text(encoding="utf-8"))
        return patch.transition if patch.admission == "accepted" else None

    def accepted_boundary_states(self) -> tuple[MaterializationResult, list[GraphState]]:
        result, boundaries = self.accepted_patch_boundaries()
        return result, [state for _previous, _patch, state in boundaries]

    def accepted_patch_boundaries(
        self,
    ) -> tuple[MaterializationResult, list[tuple[GraphState, Patch, GraphState]]]:
        engine = self.history_engine
        generation = engine.snapshot(self.graph_target)
        assert generation.source.metadata is not None
        self._validate_metadata(generation.source.metadata)
        return generation.result, [
            (step.before_state, step.patch, step.after_state)
            for step in engine._steps(generation, generation.head(generation.result.state.revision))
            if step.accepted
        ]

    def revision_summaries(
        self,
        from_revision: int = 1,
        to_revision: int | None = None,
    ) -> list[dict[str, object]]:
        engine = self.history_engine
        generation = engine.snapshot(self.graph_target)
        assert generation.source.metadata is not None
        self._validate_metadata(generation.source.metadata)
        latest = generation.result.state.revision
        end = latest if to_revision is None else min(to_revision, latest)
        if end < generation.initial_head.revision:
            return []
        return [
            item.model_dump(mode="json")
            for item in build_revision_summaries(
                engine._steps(generation, generation.head(end), from_revision=from_revision)
            )
        ]

    def slice(self, from_revision: int, to_revision: int | None = None) -> list[dict[str, object]]:
        generation = self.history_engine.snapshot(self.graph_target)
        assert generation.source.metadata is not None
        self._validate_metadata(generation.source.metadata)
        base = generation.initial_head.revision
        stop = len(generation.index) if to_revision is None else max(0, to_revision - base)
        steps = generation.index[max(0, from_revision - base - 1) : stop]
        return [
            {
                "revision": step.patch.revision,
                "kind": step.patch.kind,
                "created_at": step.patch.created_at.isoformat(),
                "summary": step.patch.summary,
                "change_summary": step.patch.change_summary,
            }
            for step in steps
        ]

    def merge_receipts(self) -> list[BranchMergeReceipt]:
        with self._process_lock:
            receipts: list[BranchMergeReceipt] = []
            for path in self._merge_paths():
                _require_regular_file(path, "branch merge receipt")
                receipt = BranchMergeReceipt.model_validate_json(path.read_text(encoding="utf-8"))
                self._require_receipt_identity(receipt)
                if path.stem != receipt.provenance.merge_id:
                    raise ValueError(
                        f"branch merge receipt name disagrees with its content: {path}"
                    )
                receipts.append(receipt)
            return receipts

    def validated_merge_receipts(self) -> list[BranchMergeReceipt]:
        """Read every retained receipt and verify it against branch and main replay."""

        with self._process_lock:
            branch = self.history_engine.materialization(self.graph_target, refresh=False)
            main = self.history_engine.materialization(GraphTargetRef(), refresh=False)
            self.require_writable(branch.state)
            self.parent.require_writable(main.state)
            return [
                self._read_validated_merge_receipt(path, branch, main)
                for path in self._merge_paths()
            ]

    def write_merge_receipt(self, receipt: BranchMergeReceipt) -> BranchMergeReceipt:
        self._require_receipt_identity(receipt)
        with self.workspace.transaction(), self.parent._append_lock():
            self._metadata = self._read_metadata()
            self.ensure_layout()
            if receipt.provenance.branch_base_head != self._metadata.base_head:
                raise ValueError("branch merge receipt names a different immutable main base")
            current = self.materialize(write_outputs=False)
            current = self._repair_materializations_locked(current)
            if receipt.provenance.branch_head != self._head_at_revision(
                current,
                receipt.provenance.branch_head.revision,
            ):
                raise ValueError("branch merge receipt does not name an exact branch head")
            main = self.parent.materialize(write_outputs=False)
            self.parent.require_writable(main.state)
            self._validate_merge_receipt_source(receipt, current, main)
            derived = [
                item
                for item in _main_merge_receipts(main).get(self.branch_id, ())
                if item.provenance.merge_id == receipt.provenance.merge_id
            ]
            if derived:
                receipt = derived[0]
                self._validate_merge_receipt(receipt, current, main)
            target = self.merges_dir / f"{receipt.provenance.merge_id}.json"
            payload = receipt.model_dump_json(indent=2) + "\n"
            if os.path.lexists(target):
                winner = self._read_validated_merge_receipt(target, current, main)
                if winner == receipt or _same_no_change_source(winner, receipt):
                    return winner
                raise ValueError("branch merge receipt already exists with inconsistent lineage")
            self._validate_merge_receipt(
                receipt,
                current,
                main,
                require_live_authority=True,
            )
            self.parent._atomic_text(target, payload)
            relative = target.relative_to(self.parent.root)
            try:
                self.workspace.publish_committed_branch_file([relative], relative)
            except Exception as exc:
                if not self.workspace.remote:
                    raise
                status = exc.commit_status if isinstance(exc, BatchPublishFailed) else "unknown"
                if status == "present":
                    self.workspace.refresh()
                    self._metadata = self._read_metadata()
                    current = self.materialize(write_outputs=False)
                    main = self.parent.materialize(write_outputs=False)
                    winner = self._read_validated_merge_receipt(target, current, main)
                    if winner == receipt or _same_no_change_source(winner, receipt):
                        return winner
                    raise ValueError(
                        "remote branch merge receipt has inconsistent lineage"
                    ) from exc
                if os.path.lexists(target):
                    target.unlink()
                raise
            return receipt

    def _read_validated_merge_receipt(
        self,
        path: Path,
        branch: MaterializationResult,
        main: MaterializationResult,
    ) -> BranchMergeReceipt:
        _require_regular_file(path, "branch merge receipt")
        existing = BranchMergeReceipt.model_validate_json(path.read_text(encoding="utf-8"))
        if path.stem != existing.provenance.merge_id:
            raise ValueError("branch merge receipt name disagrees with its content")
        self._validate_merge_receipt(existing, branch, main)
        return existing

    def _validate_merge_receipt(
        self,
        receipt: BranchMergeReceipt,
        branch: MaterializationResult,
        main: MaterializationResult,
        *,
        require_live_authority: bool = False,
    ) -> None:
        self._validate_merge_receipt_source(receipt, branch, main)
        derived = [
            item
            for item in _main_merge_receipts(main).get(self.branch_id, ())
            if item.provenance.merge_id == receipt.provenance.merge_id
        ]
        if receipt.outcome == "committed":
            if derived != [receipt]:
                raise ValueError("committed branch merge receipt disagrees with main provenance")
        elif derived:
            raise ValueError("no-change branch merge receipt conflicts with main provenance")
        elif require_live_authority:
            self.parent._require_no_change_merge_authority(receipt)

    def _validate_merge_receipt_source(
        self,
        receipt: BranchMergeReceipt,
        branch: MaterializationResult,
        main: MaterializationResult,
    ) -> None:
        self.require_writable(branch.state)
        self.parent.require_writable(main.state)
        self.parent.head_ref(main)
        exact_metadata = self._metadata.model_copy(update={"head": self.head_ref(branch)})
        main_heads = _main_heads_by_revision(main)
        _validate_summary_receipt(self, branch, exact_metadata, receipt, main_heads)

    def reconcile_merge_receipt(self, merge_id: str) -> BranchMergeReceipt | None:
        if not re.fullmatch(r"[a-f0-9]{64}", merge_id):
            raise ValueError("branch merge id must be a lowercase SHA-256 digest")
        existing = self.merges_dir / f"{merge_id}.json"
        if os.path.lexists(existing):
            with self.workspace.transaction(), self.parent._append_lock():
                self._metadata = self._read_metadata()
                branch = self.materialize(write_outputs=False)
                main = self.parent.materialize(write_outputs=False)
                return self._read_validated_merge_receipt(existing, branch, main)
        main = self.parent.current_materialization()
        matches = [
            patch
            for patch in main.patches
            if patch.admission == "accepted"
            and patch.branch_merge is not None
            and patch.branch_merge.merge_id == merge_id
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError("main history contains duplicate branch merge provenance")
        patch = matches[0]
        assert patch.branch_merge is not None
        if patch.transition is None or patch.authorized_by is None:
            raise ValueError("committed main branch merge lacks its transition or authorizer")
        receipt = BranchMergeReceipt(
            outcome="committed",
            provenance=patch.branch_merge,
            result_main_head=GraphHeadRef(
                revision=patch.revision,
                transition_id=patch.transition.transition_id,
            ),
            authorized_by=patch.authorized_by,
            created_at=patch.created_at,
        )
        return self.write_merge_receipt(receipt)

    def _require_branch_patch(self, patch: Patch) -> None:
        if patch.kind == "identity" or patch.project_identity is not None:
            raise ValueError("project identity patches target main only")
        if patch.branch_merge is not None:
            raise ValueError("a branch merge Patch targets main, never its source branch")
        if patch.processed_cursors:
            raise ValueError("branch graph patches cannot advance provider-log cursors")
        if any(isinstance(operation, SetProjectTruthScopeOperation) for operation in patch.ops):
            raise ValueError("project truth-scope changes target main only")

    def _require_prepared_branch_patch(self, patch: Patch) -> None:
        if patch.transition is not None and patch.transition.pre_head.target != self.graph_target:
            raise ValueError("prepared branch Patch names a different graph target")
        # Human Sync and ordinary Work also write branches. Admission supplies
        # their human/task attribution and exact target; an operational episode
        # id belongs only to work actually owned by that episode.

    def _require_receipt_identity(self, receipt: BranchMergeReceipt) -> None:
        provenance = receipt.provenance
        if provenance.branch_id != self.branch_id or provenance.episode_id != self.branch_id:
            raise ValueError("branch merge receipt belongs to a different episode branch")

    def _head_at_revision(
        self,
        result: MaterializationResult,
        revision: int,
    ) -> GraphHeadRef:
        if not self._metadata.base_head.revision <= revision <= result.state.revision:
            raise ValueError("branch merge receipt names a revision outside branch history")
        transition_id = self._metadata.base_head.transition_id
        for patch in result.patches:
            if patch.revision <= self._metadata.base_head.revision:
                continue
            if patch.revision > revision:
                break
            if patch.admission == "accepted" and patch.transition is not None:
                transition_id = patch.transition.transition_id
        return GraphHeadRef(
            target=self.graph_target,
            revision=revision,
            transition_id=transition_id,
        )

    def _read_metadata(self) -> GraphBranchMetadata:
        path = self.root / "branch.json"
        try:
            _require_regular_file(path, "graph branch metadata")
            metadata = GraphBranchMetadata.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise KeyError(self.branch_id) from None
        self._validate_metadata(metadata)
        return metadata

    def _validate_metadata(self, metadata: GraphBranchMetadata) -> None:
        if metadata.branch_id != self.branch_id:
            raise ValueError("graph branch path disagrees with branch.json identity")
        if metadata.model_copy(update={"head": self._identity_metadata.head}) != (
            self._identity_metadata
        ):
            raise ValueError("graph branch immutable metadata changed after it was opened")

    def _write_metadata(self, metadata: GraphBranchMetadata) -> None:
        self.parent._atomic_text(
            self.root / "branch.json", metadata.model_dump_json(indent=2) + "\n"
        )

    def _write_materialized_outputs(self, result: MaterializationResult) -> None:
        self.parent._atomic_json(self.root / "graph.json", result.state.model_dump(mode="json"))
        self.parent._atomic_json(
            self.root / "glossary.json",
            {key: value.model_dump(mode="json") for key, value in result.state.glossary.items()},
        )
        self.parent._atomic_json(
            self.root / "proposals.json",
            {key: value.model_dump(mode="json") for key, value in result.state.proposals.items()},
        )
        self.parent._atomic_text(self.root / "research.md", render_research_md(result.state))

    def _write_committed_materializations(self, result: MaterializationResult) -> None:
        self._metadata = self._metadata.model_copy(update={"head": self.head_ref(result)})
        self._write_metadata(self._metadata)
        self._write_materialized_outputs(result)

    def _repair_materializations_locked(
        self,
        current: MaterializationResult,
    ) -> MaterializationResult:
        if not self.parent._branch_materialization_repair_required(self.branch_id):
            return current
        self.require_writable(current.state)
        try:
            self._write_committed_materializations(current)
            self.workspace.publish(self._published_paths(include_metadata=True))
        except Exception:
            self.parent._require_branch_materialization_repair(self.branch_id)
            raise
        self.parent._complete_branch_materialization_repair(self.branch_id)
        return current

    def _outputs_coherent(self, result: MaterializationResult) -> bool:
        expected = {
            "graph.json": result.state.model_dump(mode="json"),
            "glossary.json": {
                key: value.model_dump(mode="json") for key, value in result.state.glossary.items()
            },
            "proposals.json": {
                key: value.model_dump(mode="json") for key, value in result.state.proposals.items()
            },
        }
        try:
            for name in [*expected, "research.md"]:
                _require_regular_file(self.root / name, "branch materialized output")
            if any(
                json.loads((self.root / name).read_text(encoding="utf-8")) != value
                for name, value in expected.items()
            ):
                return False
            return (self.root / "research.md").read_text(encoding="utf-8") == render_research_md(
                result.state
            )
        except (OSError, ValueError):
            return False

    def _published_paths(self, *, include_metadata: bool) -> list[Path]:
        names = ["graph.json", "glossary.json", "proposals.json", "research.md"]
        if include_metadata:
            names.append("branch.json")
        return [(self.root / name).relative_to(self.parent.root) for name in names]

    def _patch_paths(self) -> list[Path]:
        return sorted(
            [
                path
                for path in _regular_directory_entries(
                    self.patches_dir,
                    "graph branch patches path",
                )
                if _PATCH_NAME.fullmatch(path.name)
            ],
            key=lambda path: int(path.stem),
        )

    def _merge_paths(self) -> list[Path]:
        return sorted(
            [
                path
                for path in _regular_directory_entries(
                    self.merges_dir,
                    "graph branch merges path",
                )
                if _MERGE_NAME.fullmatch(path.name)
            ],
            key=lambda path: path.name,
        )

    def _next_revision(self) -> int:
        paths = self._patch_paths()
        return int(paths[-1].stem) + 1 if paths else self._metadata.base_head.revision + 1

    def _reconcile_patch_publish_failure(
        self,
        exc: Exception,
        target: Path,
    ) -> bool:
        status = exc.commit_status if isinstance(exc, BatchPublishFailed) else "unknown"
        if status == "present":
            self.parent._require_branch_materialization_repair(self.branch_id)
            return True
        if os.path.lexists(target):
            if status == "unknown":
                quarantine = self.patches_dir / f".unconfirmed-{target.name}-{uuid.uuid4().hex}"
                os.replace(target, quarantine)
                self.parent._require_branch_materialization_repair(self.branch_id)
            else:
                target.unlink()
        repaired = self.materialize(write_outputs=False)
        try:
            self._write_committed_materializations(repaired)
        except Exception:
            self.parent._require_branch_materialization_repair(self.branch_id)
            raise
        return False


def _same_no_change_source(
    winner: BranchMergeReceipt,
    contender: BranchMergeReceipt,
) -> bool:
    if winner.outcome != "no_change" or contender.outcome != "no_change":
        return False
    left = winner.provenance
    right = contender.provenance
    return (
        left.merge_id == right.merge_id
        and left.branch_id == right.branch_id
        and left.episode_id == right.episode_id
        and left.branch_base_head == right.branch_base_head
        and left.branch_head == right.branch_head
    )


def existing_receipt_for_main_append(
    parent: HistoryManager,
    main: MaterializationResult,
    provenance: BranchMergeProvenance,
) -> BranchMergeReceipt | None:
    """Read the no-change winner while the caller holds the main append transaction."""

    root = _safe_branch_root(parent.root, provenance.branch_id, create=False)
    target = root / "merges" / f"{provenance.merge_id}.json"
    if not os.path.lexists(target):
        return None
    metadata_path = root / "branch.json"
    _require_regular_file(metadata_path, "graph branch metadata")
    metadata = GraphBranchMetadata.model_validate_json(metadata_path.read_text(encoding="utf-8"))
    if metadata.branch_id != provenance.branch_id or metadata.episode_id != provenance.episode_id:
        raise ValueError("branch merge receipt belongs to a different episode branch")
    _require_parent_identity(
        parent,
        metadata,
        allow_historical_home=True,
        materialization=main,
    )
    branch = BranchHistoryManager(parent, metadata)
    branch_result = branch.materialize(write_outputs=False)
    receipt = branch._read_validated_merge_receipt(target, branch_result, main)
    if receipt.outcome != "no_change":
        raise ValueError("committed branch merge receipt has no accepted main provenance")
    return receipt


def read_branch_snapshots(
    parent: HistoryManager,
    identities: list[tuple[str, str, str]],
    *,
    refresh_max_age_seconds: float = REMOTE_STATE_RECONCILE_WINDOW_SECONDS,
) -> dict[str, BranchReadSnapshot | None]:
    """Read branch heads and receipts without taking a publication transaction.

    The list endpoint can contain many historical episodes. Refreshing the remote
    mirror, replaying main, or reconciling a missing receipt once per episode would
    turn that read into a series of canonical-state transactions. This path fixes a
    single snapshot, validates main once, and derives any crash-recovery receipt in
    memory rather than publishing it from a GET.
    """

    expected: dict[str, tuple[str, str]] = {}
    for branch_id, episode_id, project_id in identities:
        canonical_branch_id(branch_id)
        identity = (episode_id, project_id)
        previous = expected.setdefault(branch_id, identity)
        if previous != identity:
            raise ValueError("one graph branch was requested with conflicting identities")

    if not expected:
        return {}

    with parent._process_lock:
        if not parent.workspace.refresh_if_stale(refresh_max_age_seconds):
            raise StateUnavailable("canonical state refresh did not confirm a current snapshot")
        metadata_contents: dict[str, bytes] = {}
        for branch_id in expected:
            path = _safe_branch_root(parent.root, branch_id, create=False) / "branch.json"
            try:
                _require_regular_file(path, "graph branch metadata")
                metadata_contents[branch_id] = path.read_bytes()
            except FileNotFoundError:
                continue
        targets = [
            GraphTargetRef(kind="branch", branch_id=branch_id) for branch_id in metadata_contents
        ]
    generations = parent.history_engine.snapshots(targets, refresh=False)
    main = generations["main"].result
    main_heads = _main_heads_by_revision(main)
    main_states = {
        generation.initial_head.revision: generation.initial
        for key, generation in generations.items()
        if key != "main"
    }
    main_receipts = _main_merge_receipts(main)
    with parent._process_lock:
        for branch_id, content in metadata_contents.items():
            path = _safe_branch_root(parent.root, branch_id, create=False) / "branch.json"
            _require_regular_file(path, "graph branch metadata")
            if path.read_bytes() != content:
                raise StateUnavailable("graph branch metadata changed during the history read")
        return {
            branch_id: _read_branch_snapshot(
                parent,
                branch_id,
                expected_episode_id=episode_id,
                expected_project_id=project_id,
                main=main,
                main_heads=main_heads,
                main_states=main_states,
                main_receipts=main_receipts.get(branch_id, ()),
                materialization=generations[
                    GraphTargetRef(kind="branch", branch_id=branch_id).key
                ].result,
            )
            if GraphTargetRef(kind="branch", branch_id=branch_id).key in generations
            else None
            for branch_id, (episode_id, project_id) in expected.items()
        }


def _read_branch_snapshot(
    parent: HistoryManager,
    branch_id: str,
    *,
    expected_episode_id: str,
    expected_project_id: str,
    main: MaterializationResult,
    main_heads: dict[int, GraphHeadRef],
    main_states: dict[int, GraphState],
    main_receipts: tuple[BranchMergeReceipt, ...],
    materialization: MaterializationResult,
) -> BranchReadSnapshot | None:
    root = _safe_branch_root(parent.root, branch_id, create=False)
    metadata_path = root / "branch.json"
    try:
        _require_regular_file(metadata_path, "graph branch metadata")
        metadata = GraphBranchMetadata.model_validate_json(
            metadata_path.read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        return None
    if metadata.branch_id != branch_id:
        raise ValueError("graph branch path disagrees with branch.json identity")
    if metadata.episode_id != expected_episode_id:
        raise ValueError("graph branch belongs to a different episode")
    if metadata.project_id != expected_project_id:
        raise ValueError("graph branch belongs to a different project")
    _require_parent_identity(
        parent,
        metadata,
        allow_historical_home=True,
        materialization=main,
    )

    exact_base = main_heads.get(metadata.base_head.revision)
    if exact_base != metadata.base_head:
        raise ValueError("graph branch immutable main base is not an exact accepted main head")

    base_state = main_states.get(metadata.base_head.revision)
    if base_state is None:
        raise ValueError("graph branch immutable main base has no coherent main state")

    branch = BranchHistoryManager(parent, metadata)
    if base_state.revision != metadata.base_head.revision:
        raise ValueError("graph branch snapshot has a different immutable main base")
    replayed = materialization
    branch.require_writable(replayed.state)
    exact_metadata = metadata.model_copy(update={"head": branch.head_ref(replayed)})

    receipts_by_id: dict[str, BranchMergeReceipt] = {}
    for receipt in branch.merge_receipts():
        _validate_summary_receipt(branch, replayed, exact_metadata, receipt, main_heads)
        receipts_by_id[receipt.provenance.merge_id] = receipt
    for receipt in main_receipts:
        _validate_summary_receipt(branch, replayed, exact_metadata, receipt, main_heads)
        merge_id = receipt.provenance.merge_id
        persisted = receipts_by_id.get(merge_id)
        if persisted is not None and persisted != receipt:
            raise ValueError("branch merge receipt disagrees with accepted main provenance")
        receipts_by_id[merge_id] = receipt

    derived_ids = {receipt.provenance.merge_id for receipt in main_receipts}
    if any(
        receipt.outcome == "committed" and merge_id not in derived_ids
        for merge_id, receipt in receipts_by_id.items()
    ):
        raise ValueError("committed branch merge receipt has no accepted main provenance")
    receipts = tuple(sorted(receipts_by_id.values(), key=lambda item: item.created_at))
    return BranchReadSnapshot(metadata=exact_metadata, receipts=receipts, state=replayed.state)


def _main_heads_by_revision(main: MaterializationResult) -> dict[int, GraphHeadRef]:
    transition_id: str | None = None
    heads = {0: GraphHeadRef(revision=0)}
    for patch in main.patches:
        if patch.admission == "accepted" and patch.transition is not None:
            transition_id = patch.transition.transition_id
        heads[patch.revision] = GraphHeadRef(
            revision=patch.revision,
            transition_id=transition_id,
        )
    return heads


def _main_merge_receipts(
    main: MaterializationResult,
) -> dict[str, tuple[BranchMergeReceipt, ...]]:
    by_branch: dict[str, list[BranchMergeReceipt]] = {}
    seen_ids: set[str] = set()
    for patch in main.patches:
        provenance = patch.branch_merge
        if patch.admission != "accepted" or provenance is None:
            continue
        if provenance.merge_id in seen_ids:
            raise ValueError("main history contains duplicate branch merge provenance")
        seen_ids.add(provenance.merge_id)
        if patch.transition is None or patch.authorized_by is None:
            raise ValueError("committed main branch merge lacks its transition or authorizer")
        by_branch.setdefault(provenance.branch_id, []).append(
            BranchMergeReceipt(
                outcome="committed",
                provenance=provenance,
                result_main_head=GraphHeadRef(
                    revision=patch.revision,
                    transition_id=patch.transition.transition_id,
                ),
                authorized_by=patch.authorized_by,
                created_at=patch.created_at,
            )
        )
    return {
        branch_id: tuple(sorted(receipts, key=lambda item: item.created_at))
        for branch_id, receipts in by_branch.items()
    }


def _validate_summary_receipt(
    branch: BranchHistoryManager,
    structural: MaterializationResult,
    metadata: GraphBranchMetadata,
    receipt: BranchMergeReceipt,
    main_heads: dict[int, GraphHeadRef],
) -> None:
    branch._require_receipt_identity(receipt)
    provenance = receipt.provenance
    if provenance.branch_base_head != metadata.base_head:
        raise ValueError("branch merge receipt names a different immutable main base")
    receipt_metadata = metadata.model_copy(update={"head": provenance.branch_head})
    if provenance.merge_id != _branch_merge_id(receipt_metadata):
        raise ValueError("branch merge receipt id does not match its exact branch lineage")
    if provenance.branch_head != branch._head_at_revision(
        structural,
        provenance.branch_head.revision,
    ):
        raise ValueError("branch merge receipt does not name an exact branch head")
    if main_heads.get(receipt.result_main_head.revision) != receipt.result_main_head:
        raise ValueError("branch merge receipt does not name an exact main head")


def _branch_merge_id(metadata: GraphBranchMetadata) -> str:
    payload = {
        "schema_generation": 1,
        "kind": "auto_research_graph_branch_merge",
        "branch_id": metadata.branch_id,
        "episode_id": metadata.episode_id,
        "project_id": metadata.project_id,
        "branch_kind": metadata.kind,
        "branch_base_head": metadata.base_head.model_dump(mode="json"),
        "branch_head": metadata.head.model_dump(mode="json"),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_episode_branch(
    parent: HistoryManager,
    metadata: GraphBranchMetadata,
) -> BranchHistoryManager:
    canonical_branch_id(metadata.branch_id)
    _require_parent_identity(parent, metadata)
    expected_initial = GraphHeadRef(
        target=GraphTargetRef(kind="branch", branch_id=metadata.branch_id),
        revision=metadata.base_head.revision,
        transition_id=metadata.base_head.transition_id,
    )
    if metadata.head != expected_initial:
        raise ValueError("new graph branch head must begin at its immutable main base")
    with parent.workspace.transaction(), parent._append_lock():
        parent._reload_manifest()
        branches_root = _safe_branches_root(parent.root, create=True)
        root = branches_root / metadata.branch_id
        if os.path.lexists(root):
            existing = BranchHistoryManager(parent, metadata)._read_metadata()
            if existing.model_copy(update={"head": metadata.head}) != metadata:
                raise ValueError("graph branch already exists with different canonical metadata")
            return BranchHistoryManager(parent, existing)
        current = parent.materialize(write_outputs=False)
        parent.require_writable(current.state)
        parent._require_writable_home_locked(current)
        expected_base = parent.head_ref(current)
        if metadata.base_head != expected_base:
            raise ValueError("graph branch base must be the exact current main head")
        root.mkdir(mode=0o700)
        branch = BranchHistoryManager(parent, metadata)
        branch.ensure_layout()
        branch._write_metadata(metadata)
        branch._write_materialized_outputs(current)
        relative_metadata = (root / "branch.json").relative_to(parent.root)
        paths = [relative_metadata, *branch._published_paths(include_metadata=False)]
        try:
            parent.workspace.publish_committed_branch_file(paths, relative_metadata)
        except Exception as exc:
            if not parent.workspace.remote:
                shutil.rmtree(root)
                raise
            status = exc.commit_status if isinstance(exc, BatchPublishFailed) else "unknown"
            if status == "present":
                parent.workspace.refresh()
                if (root / "branch.json").is_file():
                    stored = GraphBranchMetadata.model_validate_json(
                        (root / "branch.json").read_text(encoding="utf-8")
                    )
                    if stored == metadata:
                        return BranchHistoryManager(parent, stored)
                raise ValueError(
                    "remote graph branch commit disagrees with requested metadata"
                ) from exc
            if root.exists():
                if status == "unknown":
                    os.replace(
                        root,
                        branches_root / f".unconfirmed-{metadata.branch_id}-{uuid.uuid4().hex}",
                    )
                else:
                    shutil.rmtree(root)
            raise
        return branch


def open_branch(
    parent: HistoryManager,
    branch_id: str,
    *,
    expected_episode_id: str | None = None,
    expected_project_id: str | None = None,
    initialize: bool = True,
    refresh_max_age_seconds: float = REMOTE_STATE_RECONCILE_WINDOW_SECONDS,
) -> BranchHistoryManager:
    canonical_branch_id(branch_id)
    main = (
        parent.history_engine.materialization(
            GraphTargetRef(), refresh_max_age_seconds=refresh_max_age_seconds
        )
        if not initialize
        else None
    )
    with parent._process_lock:
        root = _safe_branch_root(parent.root, branch_id, create=False)
        try:
            _require_regular_file(root / "branch.json", "graph branch metadata")
            metadata = GraphBranchMetadata.model_validate_json(
                (root / "branch.json").read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            raise KeyError(branch_id) from None
        if metadata.branch_id != branch_id:
            raise ValueError("graph branch path disagrees with branch.json identity")
        if expected_episode_id is not None and metadata.episode_id != expected_episode_id:
            raise ValueError("graph branch belongs to a different episode")
        if expected_project_id is not None and metadata.project_id != expected_project_id:
            raise ValueError("graph branch belongs to a different project")
        _require_parent_identity(
            parent,
            metadata,
            allow_historical_home=True,
            materialization=main,
        )
        branch = BranchHistoryManager(parent, metadata)
        branch._metadata = branch._read_metadata()
        if initialize:
            branch.initialize()
        return branch


def _require_parent_identity(
    parent: HistoryManager,
    metadata: GraphBranchMetadata,
    *,
    allow_historical_home: bool = False,
    materialization: MaterializationResult | None = None,
) -> None:
    if parent.project_id is not None and metadata.project_id != parent.project_id:
        raise ValueError("graph branch belongs to a different project")
    expected_space_id = (
        parent.project_home_space_id_at_revision(
            metadata.base_head.revision,
            materialization=materialization,
        )
        if allow_historical_home
        else parent.expected_space_id
    )
    if expected_space_id is not None and metadata.authorized_by.space_id != expected_space_id:
        raise ValueError("graph branch authorizer belongs to a different space")


def _safe_branches_root(root: Path, *, create: bool) -> Path:
    branches = root / "branches"
    if os.path.lexists(branches):
        if not stat.S_ISDIR(branches.lstat().st_mode):
            raise ValueError("canonical graph branches path is not a regular directory")
    elif create:
        branches.mkdir(mode=0o700)
    return branches


def _safe_branch_root(root: Path, branch_id: str, *, create: bool) -> Path:
    canonical_branch_id(branch_id)
    branches = _safe_branches_root(root, create=create)
    branch = branches / branch_id
    if os.path.lexists(branch):
        if not stat.S_ISDIR(branch.lstat().st_mode):
            raise ValueError("canonical graph branch path is not a regular directory")
    elif create:
        branch.mkdir(mode=0o700)
    return branch


def _require_regular_file(path: Path, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ValueError(f"could not inspect {label} at {path}: {exc}") from exc
    if not stat.S_ISREG(mode):
        raise ValueError(f"{label} is not a regular file: {path}")


def _regular_directory_entries(path: Path, label: str) -> list[Path]:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ValueError(f"could not inspect {label} at {path}: {exc}") from exc
    if not stat.S_ISDIR(mode):
        raise ValueError(f"{label} is not a regular directory: {path}")
    try:
        return list(path.iterdir())
    except OSError as exc:
        raise ValueError(f"could not enumerate {label} at {path}: {exc}") from exc
