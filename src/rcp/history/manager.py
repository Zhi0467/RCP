from __future__ import annotations

import fcntl
import json
import os
import shutil
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from rcp.config import (
    AgentSurface,
    AgentSurfaceConfig,
    Manifest,
    load_manifest,
    validate_project_scope_update,
    write_agent_settings,
    write_machine_provider_paths,
    write_project_scope,
)
from rcp.core.materialize import (
    AcceptedPatchObserver,
    MaterializationResult,
    apply_valid_patch,
    materialize_patches,
    prepare_patch_bookkeeping,
)
from rcp.core.models import GraphState, Patch
from rcp.core.research_md import render_research_md
from rcp.core.validation import ValidationReport, validate_patch
from rcp.history.delta import (
    RefreshDelta,
    RevisionSummary,
    build_refresh_delta,
    render_revision_summary,
)
from rcp.providers import ProviderId
from rcp.skill_registry import SkillDefaults
from rcp.transport import (
    BatchPublishFailed,
    LocalStateWorkspace,
    StateUnavailable,
    StateWorkspace,
)


class RevisionConflict(ValueError):
    """A patch was written against a graph revision that is no longer current."""


class ReplayHalted(RuntimeError):
    """Canonical history is structurally invalid and therefore read-only."""

    def __init__(self, state: GraphState) -> None:
        failure = state.replay_failure
        if failure is None:
            message = (
                f"Canonical replay is degraded at coherent revision {state.revision}; "
                "history must be repaired before making canonical changes."
            )
            self.failed_revision = None
            self.code = "replay-halted"
        else:
            message = (
                f"Canonical replay halted at revision {failure.revision} "
                f"({failure.code}): {failure.message} The graph is read-only at "
                f"coherent revision {state.revision} until history is repaired."
            )
            self.failed_revision = failure.revision
            self.code = failure.code
        self.coherent_revision = state.revision
        super().__init__(message)


class PatchRejected(ValueError):
    def __init__(self, report: ValidationReport) -> None:
        self.report = report
        super().__init__(
            "; ".join(item.message for item in report.messages if item.level == "reject")
        )


class HistoryManager:
    def __init__(
        self,
        manifest: Manifest,
        workspace: StateWorkspace | None = None,
    ) -> None:
        self.manifest = manifest
        self.workspace = workspace or LocalStateWorkspace(
            manifest.research_dir, str(manifest.research_dir)
        )
        self.root = self.workspace.root
        self.patches_dir = self.root / "patches"
        self._process_lock = self.workspace.snapshot_lock
        self._accepted_revision: int | None = None

    def initialize(self) -> MaterializationResult:
        with self._process_lock:
            self._reload_manifest()
            coherent = self._coherent_materialization()
            if coherent is not None:
                self._remember_accepted_revision(coherent)
                return coherent

        publishing = False
        try:
            with self.workspace.transaction(), self._append_lock():
                self._reload_manifest()
                coherent = self._coherent_materialization()
                if coherent is not None:
                    self._remember_accepted_revision(coherent)
                    return coherent
                self.ensure_layout()
                result = self.materialize()
                self._synchronize_manifest_from_history(result)
                publishing = True
                self.workspace.publish(self._materialized_paths(include_manifest=True))
                self.workspace.complete_materialization_repair()
                self._remember_accepted_revision(result)
                return result
        except StateUnavailable:
            if publishing and self.workspace.remote:
                self.workspace.require_materialization_repair()
            if not (self.root / "manifest.toml").is_file():
                raise
            with self._append_lock():
                self._reload_manifest()
                self.ensure_layout()
                result = self.materialize()
                self._synchronize_manifest_from_history(result)
                self._remember_accepted_revision(result)
                return result

    def ensure_layout(self) -> None:
        for path in (
            self.patches_dir,
            self.root / "chat",
            self.root / "paper",
            self.root / "facts",
        ):
            path.mkdir(parents=True, exist_ok=True)
        defaults = {
            "graph.json": GraphState(
                project_truth_scope=self.manifest.project.truth_scope
            ).model_dump(mode="json"),
            "glossary.json": {},
            "proposals.json": {},
            "coverage.json": GraphState().coverage.model_dump(mode="json"),
            "cursors.json": {},
            "scope-base.json": {
                "truth_scope": self.manifest.project.truth_scope,
                "repository_aliases": sorted(self.manifest.repository_map),
            },
        }
        for name, value in defaults.items():
            path = self.root / name
            if not path.exists():
                self._atomic_json(path, value)
        research_md = self.root / "research.md"
        if not research_md.exists():
            self._atomic_text(research_md, "")

    def load_patches(self) -> list[Patch]:
        with self._process_lock:
            return [
                Patch.model_validate_json(path.read_text(encoding="utf-8"))
                for path in self._patch_paths()
            ]

    def current_accepted_revision(self) -> int:
        """Return the cached accepted revision without reading canonical patch bodies."""

        with self._process_lock:
            if self._accepted_revision is None:
                # Project services call ``initialize`` before exposure. This fallback
                # keeps a directly constructed HistoryManager correct without making
                # the steady-state API probe replay or read patch files.
                self._remember_accepted_revision(self.materialize(write_outputs=False))
            assert self._accepted_revision is not None
            return self._accepted_revision

    def state(self) -> GraphState:
        return self.current_materialization().state

    def current_materialization(self) -> MaterializationResult:
        """Replay current canonical history once and return state plus reports."""

        with self._process_lock:
            self._repair_materializations_if_needed()
            with suppress(StateUnavailable):
                self.workspace.refresh_if_stale()
            self._reload_manifest()
            result = self.materialize(write_outputs=False)
            self._remember_accepted_revision(result)
            return result

    def require_writable(self, state: GraphState | None = None) -> GraphState:
        """Return the coherent state, or refuse a canonical mutation after replay halts."""

        current = state or self.current_materialization().state
        if current.replay_status == "degraded":
            raise ReplayHalted(current)
        return current

    def append(
        self,
        patch: Patch,
        *,
        raise_on_reject: bool = True,
        discard_on_reject: bool = False,
        expected_revision: int | None = None,
    ) -> tuple[Patch, MaterializationResult]:
        """Append a patch to the log and rematerialize.

        A rejected patch is still written to the append-only log. Callers that
        want to inspect the report themselves pass ``raise_on_reject=False``.
        Agent workflows that must correct an invalid deliverable before it
        enters canonical history pass ``discard_on_reject=True``; validation
        still happens under the append lock, but the rejected candidate is not
        written and does not consume a revision.

        ``expected_revision`` refuses a patch written against state that has since
        moved. The comparison happens under the append lock, which is the same
        lock every other writer takes, so nothing can land between the check and
        the write — a freshness check made outside this lock cannot say that.
        """
        with self.workspace.transaction(), self._append_lock():
            self._reload_manifest()
            self.ensure_layout()
            self._repair_materializations_locked()
            current = self.materialize(write_outputs=False)
            self.require_writable(current.state)
            if expected_revision is not None and current.state.revision != expected_revision:
                raise RevisionConflict(
                    f"the graph moved from revision {expected_revision} to "
                    f"{current.state.revision} while this patch was being written"
                )
            revision = self._next_revision()
            patch, report, _preflight_state = self._validate_candidate_locked(
                current,
                patch,
                revision,
            )
            if discard_on_reject and report.rejected:
                raise PatchRejected(report)
            patch = patch.model_copy(
                update={
                    "admission": "rejected" if report.rejected else "accepted",
                    "admission_messages": list(report.messages),
                }
            )
            target = self.patches_dir / f"{revision:06d}.json"
            manifest_path = self.root / "manifest.toml"
            manifest_before = manifest_path.read_text(encoding="utf-8")
            self._atomic_text(target, patch.model_dump_json(indent=2) + "\n")
            result = self.materialize(write_outputs=True)
            scope_changed = False
            if not result.reports[revision].rejected:
                scope_changed = self._synchronize_manifest_scope(result, patch)
            paths = [target.relative_to(self.root), *self._materialized_paths()]
            if scope_changed:
                paths.append(Path("manifest.toml"))
            try:
                self.workspace.publish_committed_patch(
                    paths,
                    target.relative_to(self.root),
                )
            except Exception as exc:
                if not self.workspace.remote:
                    raise
                if not self._reconcile_remote_publish_failure(
                    exc,
                    target,
                    scope_changed=scope_changed,
                    manifest_before=manifest_before,
                ):
                    raise
            self._remember_accepted_revision(result)
            if raise_on_reject and result.reports[revision].rejected:
                raise PatchRejected(result.reports[revision])
            return patch, result

    def validate_candidate(self, patch: Patch) -> tuple[Patch, ValidationReport, GraphState]:
        """Validate without writing, against canonical state held under the append lock."""

        with self.workspace.transaction(), self._append_lock():
            self._reload_manifest()
            self.ensure_layout()
            self._repair_materializations_locked()
            current = self.materialize(write_outputs=False)
            self.require_writable(current.state)
            prepared, report, _candidate = self._validate_candidate_locked(
                current,
                patch,
                self._next_revision(),
            )
            return prepared, report, current.state

    def _validate_candidate_locked(
        self,
        current: MaterializationResult,
        patch: Patch,
        revision: int,
    ) -> tuple[Patch, ValidationReport, GraphState | None]:
        patch = patch.model_copy(update={"revision": revision})
        patch = prepare_patch_bookkeeping(current.state, patch)
        report = validate_patch(
            current.state,
            patch,
            current.state.project_truth_scope,
            repository_aliases=self.manifest.repository_map,
            machine_aliases=self.manifest.machine_map,
            default_run_truth_scope=self.manifest.agent.default_run_truth_scope,
            state_repository=self.manifest.state.repository,
        )
        preflight_state: GraphState | None = None
        if not report.rejected:
            try:
                preflight_state = apply_valid_patch(current.state, patch)
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                report.reject(
                    "malformed-operation",
                    f"Patch operations could not be applied atomically: {exc}.",
                    revision,
                )
        if (
            preflight_state is not None
            and preflight_state.project_truth_scope != current.state.project_truth_scope
        ):
            descriptor = next(
                (
                    op.get("repository")
                    for op in patch.ops
                    if op.get("op") == "set_project_truth_scope" and op.get("repository")
                ),
                None,
            )
            try:
                validate_project_scope_update(
                    self.manifest,
                    preflight_state.project_truth_scope,
                    descriptor,
                )
            except ValueError as exc:
                report.reject("invalid-project-scope", str(exc), revision)
        return patch, report, preflight_state

    def update_agent_settings(
        self,
        default_run_truth_scope: list[str],
        profiles: dict[AgentSurface, AgentSurfaceConfig],
        provider_path_updates: dict[str, dict[ProviderId, str]] | None = None,
        skill_defaults: SkillDefaults | None = None,
    ) -> Manifest:
        with self.workspace.transaction(), self._append_lock():
            self._reload_manifest()
            self._repair_materializations_locked()
            self.require_writable(self.materialize(write_outputs=False).state)
            self.manifest = write_agent_settings(
                self.manifest,
                default_run_truth_scope,
                profiles,
                provider_path_updates,
                skill_defaults,
            )
            self.workspace.publish([Path("manifest.toml")])
        return self.manifest

    def update_machine_provider_paths(
        self,
        provider_path_updates: dict[str, dict[ProviderId, str]],
    ) -> Manifest:
        with self.workspace.transaction(), self._append_lock():
            self._reload_manifest()
            self._repair_materializations_locked()
            self.require_writable(self.materialize(write_outputs=False).state)
            self.manifest = write_machine_provider_paths(
                self.manifest,
                provider_path_updates,
            )
            self.workspace.publish([Path("manifest.toml")])
        return self.manifest

    def append_batch(
        self,
        patches: list[Patch],
        *,
        expected_revision: int | None = None,
    ) -> tuple[list[Patch], MaterializationResult]:
        """Append a validated human transaction and publish materializations once."""

        if not patches:
            return [], self.materialize(write_outputs=False)
        return self.append_batch_from_state(
            lambda _state: patches,
            expected_revision=expected_revision,
        )

    def append_batch_from_state(
        self,
        build_patches: Callable[[GraphState], list[Patch]],
        *,
        expected_revision: int | None = None,
    ) -> tuple[list[Patch], MaterializationResult]:
        """Build and append a human transaction from the fresh, append-locked state."""

        with self.workspace.transaction(), self._append_lock():
            self._reload_manifest()
            self.ensure_layout()
            self._repair_materializations_locked()
            current = self.materialize(write_outputs=False)
            self.require_writable(current.state)
            if expected_revision is not None and current.state.revision != expected_revision:
                raise ValueError(
                    "The graph changed after this draft began; reload before syncing it."
                )
            patches = build_patches(current.state)
            if not patches:
                return [], current
            state = current.state
            next_revision = self._next_revision()
            prepared: list[Patch] = []
            for offset, raw_patch in enumerate(patches):
                patch = raw_patch.model_copy(update={"revision": next_revision + offset})
                patch = prepare_patch_bookkeeping(state, patch)
                report = validate_patch(
                    state,
                    patch,
                    state.project_truth_scope,
                    repository_aliases=self.manifest.repository_map,
                    machine_aliases=self.manifest.machine_map,
                    default_run_truth_scope=self.manifest.agent.default_run_truth_scope,
                    state_repository=self.manifest.state.repository,
                )
                if report.rejected:
                    raise PatchRejected(report)
                try:
                    candidate = apply_valid_patch(state, patch)
                except (AttributeError, KeyError, TypeError, ValueError) as exc:
                    report.reject(
                        "malformed-operation",
                        f"Patch operations could not be applied atomically: {exc}.",
                        patch.revision,
                    )
                    raise PatchRejected(report) from exc
                patch = patch.model_copy(
                    update={
                        "admission": "accepted",
                        "admission_messages": list(report.messages),
                    }
                )
                state = candidate
                prepared.append(patch)

            batch_name = (
                f"batch-{prepared[0].revision:06d}-{prepared[-1].revision:06d}-{uuid.uuid4().hex}"
            )
            staging = self.patches_dir / f".{batch_name}"
            committed = self.patches_dir / batch_name
            manifest_path = self.root / "manifest.toml"
            manifest_before = manifest_path.read_text(encoding="utf-8")
            staging.mkdir(mode=0o700)
            pending_paths: list[Path] = []
            batch_committed = False
            try:
                for patch in prepared:
                    target = staging / f"{patch.revision:06d}.json"
                    self._atomic_text(
                        target,
                        patch.model_dump_json(indent=2) + "\n",
                    )
                    pending_paths.append(target)
                result = self.materialize(
                    write_outputs=False,
                    pending_patch_paths=pending_paths,
                )
                os.replace(staging, committed)
                self._fsync_directory(self.patches_dir)
                batch_committed = True
                self._write_materialized_outputs(result)
                scope_changed = False
                for patch in prepared:
                    if self._synchronize_manifest_scope(result, patch):
                        scope_changed = True
                targets = [(committed / path.name).relative_to(self.root) for path in pending_paths]
                paths = [*targets, *self._materialized_paths()]
                if scope_changed:
                    paths.append(Path("manifest.toml"))
                try:
                    self.workspace.publish_committed_batch(
                        paths,
                        committed.relative_to(self.root),
                    )
                except Exception as exc:
                    if not self.workspace.remote:
                        raise
                    if not self._reconcile_remote_publish_failure(
                        exc,
                        committed,
                        scope_changed=scope_changed,
                        manifest_before=manifest_before,
                    ):
                        raise
                self._remember_accepted_revision(result)
                return prepared, result
            finally:
                if not batch_committed:
                    shutil.rmtree(staging, ignore_errors=True)

    def materialize(
        self,
        *,
        write_outputs: bool = True,
        pending_patch_paths: list[Path] | None = None,
        accepted_patch_observer: AcceptedPatchObserver | None = None,
    ) -> MaterializationResult:
        with self._process_lock:
            self.ensure_layout()
            if accepted_patch_observer is None:
                result = self._replay(pending_patch_paths)
            else:
                result = self._replay(
                    pending_patch_paths,
                    accepted_patch_observer=accepted_patch_observer,
                )
            if write_outputs:
                self._write_materialized_outputs(result)
            return result

    def _coherent_materialization(self) -> MaterializationResult | None:
        """Return replayed state only when every cached derived output already matches."""

        if self.workspace.materialization_repair_required:
            return None
        required = self._materialized_paths(include_manifest=True)
        if not all((self.root / path).is_file() for path in required):
            return None
        result = self._replay()
        if result.state.project_truth_scope != self.manifest.project.truth_scope:
            return None
        expected_json = {
            "graph.json": result.state.model_dump(mode="json"),
            "glossary.json": {
                key: value.model_dump(mode="json") for key, value in result.state.glossary.items()
            },
            "proposals.json": {
                key: value.model_dump(mode="json") for key, value in result.state.proposals.items()
            },
            "coverage.json": result.state.coverage.model_dump(mode="json"),
            "cursors.json": result.processed_cursors,
        }
        try:
            for name, expected in expected_json.items():
                if json.loads((self.root / name).read_text(encoding="utf-8")) != expected:
                    return None
            if (self.root / "research.md").read_text(encoding="utf-8") != render_research_md(
                result.state
            ):
                return None
        except (OSError, ValueError):
            return None
        return result

    def _replay(
        self,
        pending_patch_paths: list[Path] | None = None,
        *,
        accepted_patch_observer: AcceptedPatchObserver | None = None,
    ) -> MaterializationResult:
        pending = pending_patch_paths or []
        patch_paths = sorted(
            [*self._patch_paths(), *pending],
            key=lambda path: int(path.stem),
        )
        patches = [
            Patch.model_validate_json(path.read_text(encoding="utf-8")) for path in patch_paths
        ]
        scope_base = json.loads((self.root / "scope-base.json").read_text(encoding="utf-8"))
        return materialize_patches(
            patches,
            initial_truth_scope=list(scope_base["truth_scope"]),
            repository_aliases=sorted(self.manifest.repository_map),
            machine_aliases=sorted(self.manifest.machine_map),
            default_run_truth_scope=list(self.manifest.agent.default_run_truth_scope),
            state_repository=self.manifest.state.repository,
            accepted_patch_observer=accepted_patch_observer,
        )

    def _write_materialized_outputs(self, result: MaterializationResult) -> None:
        self._atomic_json(self.root / "graph.json", result.state.model_dump(mode="json"))
        self._atomic_json(
            self.root / "glossary.json",
            {key: value.model_dump(mode="json") for key, value in result.state.glossary.items()},
        )
        self._atomic_json(
            self.root / "proposals.json",
            {key: value.model_dump(mode="json") for key, value in result.state.proposals.items()},
        )
        self._atomic_json(
            self.root / "coverage.json", result.state.coverage.model_dump(mode="json")
        )
        self._atomic_json(self.root / "cursors.json", result.processed_cursors)
        self._atomic_text(self.root / "research.md", render_research_md(result.state))

    def refresh_delta(
        self,
        materialization: MaterializationResult | None = None,
    ) -> RefreshDelta:
        """Return the bounded delta without coupling context assembly to history I/O."""

        with self._process_lock:
            result = materialization or self.materialize(write_outputs=False)
            return build_refresh_delta(self.load_patches(), result)

    def slice(self, from_revision: int, to_revision: int | None = None) -> list[dict[str, object]]:
        with self._process_lock:
            with suppress(StateUnavailable):
                self.workspace.refresh_if_stale()
            end = to_revision if to_revision is not None else 10**12
            return [
                {
                    "revision": patch.revision,
                    "kind": patch.kind,
                    "created_at": patch.created_at.isoformat(),
                    "summary": patch.summary,
                    "change_summary": patch.change_summary,
                }
                for patch in self.load_patches()
                if from_revision <= patch.revision <= end
            ]

    def revision_summaries(
        self,
        from_revision: int = 1,
        to_revision: int | None = None,
    ) -> list[dict[str, object]]:
        """Return a reader-facing projection without changing the raw history contract."""

        with self._process_lock:
            with suppress(StateUnavailable):
                self.workspace.refresh_if_stale()
            end = to_revision if to_revision is not None else 10**12
            summaries: list[RevisionSummary] = []

            def collect(previous_state: GraphState, patch: Patch, state: GraphState) -> None:
                if from_revision <= patch.revision <= end:
                    summaries.append(render_revision_summary(previous_state, patch, state))

            self.materialize(
                write_outputs=False,
                accepted_patch_observer=collect,
            )
            return [item.model_dump(mode="json") for item in summaries]

    def _next_revision(self) -> int:
        paths = self._patch_paths()
        return int(paths[-1].stem) + 1 if paths else 1

    def _remember_accepted_revision(self, result: MaterializationResult) -> None:
        self._accepted_revision = max(
            (revision for revision, report in result.reports.items() if not report.rejected),
            default=0,
        )

    def _patch_paths(self) -> list[Path]:
        flat = list(self.patches_dir.glob("[0-9][0-9][0-9][0-9][0-9][0-9].json"))
        batched = [
            path
            for directory in self.patches_dir.glob("batch-*")
            if directory.is_dir()
            for path in directory.glob("[0-9][0-9][0-9][0-9][0-9][0-9].json")
        ]
        return sorted([*flat, *batched], key=lambda path: int(path.stem))

    def _synchronize_manifest_scope(self, result: MaterializationResult, patch: Patch) -> bool:
        if result.state.project_truth_scope == self.manifest.project.truth_scope:
            return False
        descriptor = next(
            (
                op.get("repository")
                for op in patch.ops
                if op.get("op") == "set_project_truth_scope" and op.get("repository")
            ),
            None,
        )
        self.manifest = write_project_scope(
            self.manifest,
            result.state.project_truth_scope,
            repository_descriptor=descriptor,
        )
        return True

    def _synchronize_manifest_from_history(self, result: MaterializationResult) -> bool:
        """Repair a manifest that lagged a committed truth-scope patch."""

        if result.state.project_truth_scope == self.manifest.project.truth_scope:
            return False
        descriptor = None
        for patch in reversed(self.load_patches()):
            operation = next(
                (op for op in reversed(patch.ops) if op.get("op") == "set_project_truth_scope"),
                None,
            )
            if operation is not None:
                descriptor = operation.get("repository")
                break
        self.manifest = write_project_scope(
            self.manifest,
            result.state.project_truth_scope,
            repository_descriptor=descriptor,
        )
        return True

    def _repair_materializations_if_needed(self) -> None:
        if not self.workspace.materialization_repair_required:
            return
        with self.workspace.transaction(), self._append_lock():
            self._reload_manifest()
            self.ensure_layout()
            self._repair_materializations_locked()

    def _repair_materializations_locked(self) -> None:
        if not self.workspace.materialization_repair_required:
            return
        result = self.materialize(write_outputs=True)
        self._synchronize_manifest_from_history(result)
        self.workspace.publish(self._materialized_paths(include_manifest=True))
        self.workspace.complete_materialization_repair()

    def _reconcile_remote_publish_failure(
        self,
        exc: Exception,
        committed: Path,
        *,
        scope_changed: bool,
        manifest_before: str,
    ) -> bool:
        """Reconcile the local mirror with an observed remote commit point.

        ``True`` means the history commit is confirmed and the caller should
        report success. ``False`` means the local copy has been removed from
        replay and the original publish error must be raised.
        """

        commit_status = exc.commit_status if isinstance(exc, BatchPublishFailed) else "unknown"
        if commit_status == "present":
            self.workspace.require_materialization_repair()
            return True

        if committed.exists():
            if commit_status == "unknown":
                self._quarantine_local_commit(committed)
                # A later refresh may prove that the remote commit landed. If it
                # did, derived outputs still need the same repair as a confirmed
                # post-commit failure.
                self.workspace.require_materialization_repair()
            else:
                try:
                    if committed.is_dir():
                        shutil.rmtree(committed)
                    else:
                        committed.unlink()
                except OSError:
                    # Replay safety matters more than deleting this non-canonical
                    # mirror copy immediately.
                    self._quarantine_local_commit(committed)
        self._fsync_directory(self.patches_dir)
        if scope_changed:
            self._atomic_text(self.root / "manifest.toml", manifest_before)
            self._reload_manifest()
        self.materialize(write_outputs=True)
        return False

    def _quarantine_local_commit(self, committed: Path) -> None:
        quarantine = self.patches_dir / (f".unconfirmed-{committed.name}-{uuid.uuid4().hex}")
        os.replace(committed, quarantine)

    def _reload_manifest(self) -> None:
        path = self.root / "manifest.toml"
        if path.is_file():
            self.manifest = load_manifest(path)

    @staticmethod
    def _materialized_paths(*, include_manifest: bool = False) -> list[Path]:
        paths = [
            Path("graph.json"),
            Path("glossary.json"),
            Path("proposals.json"),
            Path("coverage.json"),
            Path("cursors.json"),
            Path("scope-base.json"),
            Path("research.md"),
        ]
        if include_manifest:
            paths.append(Path("manifest.toml"))
        return paths

    @contextmanager
    def _append_lock(self) -> Iterator[None]:
        lock_path = self.root / ".append.lock"
        with self._process_lock, lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _atomic_json(path: Path, value: object) -> None:
        HistoryManager._atomic_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")

    @staticmethod
    def _atomic_text(path: Path, content: str) -> None:
        temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        HistoryManager._fsync_directory(path.parent)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def history_from_path(value: str) -> HistoryManager:
    return HistoryManager(load_manifest(value))
