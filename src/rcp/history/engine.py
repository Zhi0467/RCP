"""Verified, project-owned replay generations for graph history reads."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import sys
import threading
import weakref
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from rcp import limits
from rcp.core.materialize import (
    AcceptedPatchObserver,
    MaterializationResult,
    materialize_patches_from_state,
)
from rcp.core.models import GraphBranchMetadata, GraphState, Patch, ReplayFailure
from rcp.core.operations import SetProjectTruthScopeOperation
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.core.transitions import accepted_transition_head_chain_failure
from rcp.core.validation.settings import build_validation_context
from rcp.history.delta import semantic_delta
from rcp.history.memory_size import retained_size
from rcp.history.pages import DiffAnswer, LogAnswer
from rcp.history.queries import GraphDiffPage, GraphLogPage, HeadSpec, StepRecord
from rcp.history.state_delta import apply_state_delta, capture_state_delta, copy_state
from rcp.history.store import GraphHistoryStore, IndexedStep
from rcp.limits import REMOTE_STATE_RECONCILE_WINDOW_SECONDS
from rcp.transport import StateUnavailable

if TYPE_CHECKING:
    from rcp.config import Manifest
    from rcp.history.manager import HistoryManager

_VERSION = "graph-history-5"
_LOGGER = logging.getLogger(__name__)
_PATCH_NAME = re.compile(r"[0-9]{6}\.json")
_MEMORY_LOCK = threading.RLock()
_MEMORY: OrderedDict[tuple[int, str], weakref.ReferenceType[ProjectHistoryEngine]] = OrderedDict()


def _digest(previous: str, content: bytes) -> str:
    return hashlib.sha256(previous.encode() + len(content).to_bytes(8, "big") + content).hexdigest()


class _PatchFile(NamedTuple):
    revision: int
    path: str
    size: int
    mtime_ns: int
    inode: int
    ctime_ns: int


def _inventory(
    directory: Path, previous: tuple[_PatchFile, ...] = (), *, batches: bool = False
) -> tuple[_PatchFile, ...]:
    entries: list[_PatchFile] = []
    known = {item.path: item for item in previous}
    directories = [directory]
    for root in directories:
        if root.is_symlink():
            raise ValueError("retained Patch directory is not a regular directory")
        try:
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            if root == directory:
                return ()
            continue
        try:
            with os.scandir(descriptor) as scan:
                for entry in scan:
                    if _PATCH_NAME.fullmatch(entry.name):
                        info = entry.stat(follow_symlinks=False)
                        if not stat.S_ISREG(info.st_mode):
                            raise ValueError("retained Patch must be a regular file")
                        path = f"{root}/{entry.name}"
                        old = known.get(path)
                        if old is not None and (
                            old.size == info.st_size
                            and old.mtime_ns == info.st_mtime_ns
                            and old.inode == info.st_ino
                            and old.ctime_ns == info.st_ctime_ns
                        ):
                            entries.append(old)
                            continue
                        entries.append(
                            _PatchFile(
                                int(entry.name[:6]),
                                path,
                                info.st_size,
                                info.st_mtime_ns,
                                info.st_ino,
                                info.st_ctime_ns,
                            )
                        )
                    elif (
                        batches
                        and root == directory
                        and entry.name.startswith("batch-")
                        and entry.is_dir()
                    ):
                        directories.append(root / entry.name)
        except FileNotFoundError:
            if root == directory:
                return ()
        finally:
            os.close(descriptor)
    inventory = tuple(sorted(entries, key=lambda item: item.revision))
    return previous if inventory == previous else inventory


@dataclass(frozen=True)
class _Source:
    target: GraphTargetRef
    manifest: Manifest
    context_key: str
    scope: list[str]
    inventory: tuple[_PatchFile, ...]
    metadata: GraphBranchMetadata | None = None
    # Retained history without scope provenance reads as a degraded, empty prefix.
    scope_failure: ReplayFailure | None = None


@dataclass
class _Generation:
    target: GraphTargetRef
    context_key: str
    digests: tuple[str, ...]
    manifest: Manifest
    initial: GraphState
    initial_head: GraphHeadRef
    index: tuple[IndexedStep, ...]
    result: MaterializationResult
    source: _Source
    inherited: tuple[IndexedStep, ...] = ()
    checkpoints: dict[int, GraphState] = field(default_factory=dict)
    states: OrderedDict[int, GraphState] = field(default_factory=OrderedDict)
    state_costs: dict[int, int] = field(default_factory=dict)
    pending: list[GraphHeadRef] = field(default_factory=list)
    start: int = 0
    saved: bool = False
    store_start: int = 0

    def head(self, revision: int) -> GraphHeadRef:
        offset = revision - self.initial_head.revision
        if offset == 0:
            return self.initial_head
        if 0 < offset <= len(self.index):
            return self.index[offset - 1].after_head
        raise ValueError("history revision is absent or precedes the branch base")

    def prefix(self, head: GraphHeadRef) -> str:
        if (
            self.source.metadata is not None
            and head.revision < self.source.metadata.base_head.revision
        ):
            raise ValueError("history revision is absent or precedes the branch base")
        if self.head(head.revision) != head:
            raise ValueError("history head transition identity does not match")
        return self.digests[head.revision - self.initial_head.revision]


def _patch_failure_created_at(path: Path) -> datetime:
    """Retain useful failure chronology even when the Patch schema cannot load."""

    with suppress(OSError, TypeError, ValueError):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("created_at"), str):
            created_at = datetime.fromisoformat(raw["created_at"])
            return created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=UTC)
    with suppress(OSError):
        return datetime.fromtimestamp(path.stat().st_mtime, UTC)
    return datetime.fromtimestamp(0, UTC)


class ProjectHistoryEngine:
    """One owner shared by main and every short-lived branch service.

    Generations retain compact step indexes and endpoint states. Historical
    state dictionaries are reconstructed only for requested intervals.
    """

    def __init__(self, manager: HistoryManager) -> None:
        self.manager = manager
        self._generations: dict[str, _Generation] = {}
        self.store: GraphHistoryStore | None = None
        self._store_lock = threading.Lock()
        self.checkpoint_interval = limits.GRAPH_HISTORY_CHECKPOINT_INTERVAL
        self._branch_identities: dict[str, str] = {}
        self._verified_serial = 0
        self._published_serial = 0
        self._resolved = threading.local()
        self._retained_bytes = 0
        self._answers: OrderedDict[str, tuple[str, DiffAnswer | LogAnswer, int]] = OrderedDict()

    def configure_store(self, data_dir: Path, project_id: str) -> None:
        with self._store_lock:
            if self.store is None:
                try:
                    self.store = GraphHistoryStore(data_dir, project_id)
                except OSError:
                    _LOGGER.warning("Could not open optional graph history cache", exc_info=True)

    def _capture(
        self,
        targets: list[GraphTargetRef],
        *,
        refresh: bool = True,
        pending_patch_paths: list[Path] | None = None,
        refresh_max_age_seconds: float = REMOTE_STATE_RECONCILE_WINDOW_SECONDS,
    ) -> dict[str, _Source]:
        from rcp.history.branches import (
            _require_regular_file,
            _safe_branch_root,
            canonical_branch_id,
        )

        parent = self.manager
        if refresh and not parent.workspace.refresh_if_stale(refresh_max_age_seconds):
            raise StateUnavailable("canonical state refresh did not confirm a current snapshot")
        parent._reload_manifest()
        manifest = parent.manifest.model_copy(deep=True)
        with _MEMORY_LOCK:
            inventories = {key: value.source.inventory for key, value in self._generations.items()}
        main_inventory = _inventory(parent.patches_dir, inventories.get("main", ()), batches=True)
        if pending_patch_paths:
            tail = []
            last_revision = main_inventory[-1].revision if main_inventory else 0
            for path in sorted(pending_patch_paths, key=lambda path: int(path.stem)):
                info = path.stat(follow_symlinks=False)
                revision = int(path.stem)
                if not stat.S_ISREG(info.st_mode) or revision <= last_revision:
                    raise ValueError("pending Patches must be a regular, unpublished tail")
                tail.append(
                    _PatchFile(
                        revision,
                        str(path),
                        info.st_size,
                        info.st_mtime_ns,
                        info.st_ino,
                        info.st_ctime_ns,
                    )
                )
                last_revision = revision
            main_inventory += tuple(tail)
        scope_path = parent.root / "scope-base.json"
        scope = list(manifest.project.truth_scope)
        scope_failure = None
        failure_revision = main_inventory[0].revision if main_inventory else 0
        failure_path = Path(main_inventory[0].path) if main_inventory else scope_path
        if scope_path.is_file():
            try:
                scope = json.loads(scope_path.read_text(encoding="utf-8"))["truth_scope"]
                if not isinstance(scope, list) or not all(isinstance(item, str) for item in scope):
                    raise ValueError("truth_scope must be a list of repository aliases")
            except (OSError, KeyError, TypeError, ValueError) as exc:
                scope = list(manifest.project.truth_scope)
                scope_failure = ReplayFailure(
                    revision=failure_revision,
                    created_at=_patch_failure_created_at(failure_path),
                    code="scope-provenance-invalid",
                    message=f"Canonical scope provenance {scope_path} is invalid: {exc}",
                )
        elif main_inventory:
            scope_failure = ReplayFailure(
                revision=failure_revision,
                created_at=_patch_failure_created_at(failure_path),
                code="scope-provenance-missing",
                message=(
                    f"Canonical scope provenance {scope_path} is absent while Patch history "
                    "exists; replay is refused rather than substituting the current manifest scope."
                ),
            )
        context = build_validation_context(
            manifest, GraphState(project_truth_scope=scope), mode="replay"
        )
        key = json.dumps(
            [
                _VERSION,
                parent.project_id,
                str(parent.root.resolve()),
                scope,
                scope_failure.model_dump(mode="json") if scope_failure else None,
                sorted(context.repository_aliases or ()),
                sorted(context.machine_aliases or ()),
                sorted(context.default_run_truth_scope or ()),
                context.state_repository,
            ],
            sort_keys=True,
        )

        sources = {
            "main": _Source(
                GraphTargetRef(),
                manifest,
                key,
                scope,
                main_inventory,
                scope_failure=scope_failure,
            )
        }
        for target in targets:
            if target.kind == "main" or target.key in sources:
                continue
            assert target.branch_id is not None
            canonical_branch_id(target.branch_id)
            root = _safe_branch_root(parent.root, target.branch_id, create=False)
            try:
                _require_regular_file(root / "branch.json", "graph branch metadata")
                metadata = GraphBranchMetadata.model_validate_json(
                    (root / "branch.json").read_bytes()
                )
            except FileNotFoundError:
                raise KeyError(target.branch_id) from None
            if metadata.branch_id != target.branch_id:
                raise ValueError("branch path and identity disagree")
            if parent.project_id is not None and metadata.project_id != parent.project_id:
                raise ValueError("branch belongs to another project")
            sources[target.key] = _Source(
                target,
                manifest,
                key,
                scope,
                _inventory(root / "patches", inventories.get(target.key, ())),
                metadata,
            )
        return sources

    def _build(
        self, source: _Source, main: _Generation | None, *, use_store: bool = True
    ) -> _Generation:
        initial = GraphState(project_truth_scope=source.scope)
        initial_head = GraphHeadRef(target=source.target, revision=0)
        inherited: tuple[IndexedStep, ...] = ()
        context_key = source.context_key
        base_result = MaterializationResult(state=initial)
        if source.metadata is not None:
            assert main is not None
            from rcp.history.branches import _require_parent_identity

            identity = source.metadata.model_dump_json(exclude={"head"})
            known = self._branch_identities.get(source.target.key)
            if known is not None and known != identity:
                raise ValueError("graph branch immutable metadata changed")
            base = source.metadata.base_head
            if main.source.scope_failure is not None or base.revision > main.result.state.revision:
                base_result = main.result
            elif main.head(base.revision) != base:
                base_result = self._prefix_result(
                    main, main.head(base.revision), use_store=use_store
                )
            else:
                base_result = None
            if base_result is not None:
                # A broken inherited prefix is readable, but cannot admit branch work.
                if base_result.state.replay_status == "complete":
                    base_result = replace(
                        base_result,
                        state=base_result.state.model_copy(
                            update={
                                "replay_status": "degraded",
                                "replay_failure": ReplayFailure(
                                    revision=base.revision,
                                    created_at=source.metadata.created_at,
                                    code="branch-base-mismatch",
                                    message="Graph branch immutable main base does not match the accepted main prefix.",
                                ),
                            }
                        ),
                    )
                initial = base_result.state
                initial_head = main.head(initial.revision).model_copy(
                    update={"target": source.target}
                )
                context_key += identity + main.digests[-1]
                return _Generation(
                    source.target,
                    context_key,
                    (_digest("", context_key.encode()),),
                    source.manifest,
                    initial,
                    initial_head,
                    (),
                    base_result,
                    source,
                    inherited=main.index[: initial.revision],
                )
            _require_parent_identity(
                self.manager,
                source.metadata,
                allow_historical_home=True,
                materialization=main.result,
            )
            initial_head = base.model_copy(update={"target": source.target})
            context_key += identity + main.prefix(base)
            base_result = MaterializationResult(state=initial)
        with _MEMORY_LOCK:
            old = self._generations.get(source.target.key)
        if (
            old is not None
            and old.result.state.replay_status == "complete"
            and old.context_key == context_key
            and old.source == source
        ):
            return old
        unchanged_prefix = (
            old is not None
            and old.result.state.replay_status == "complete"
            and old.context_key == context_key
            and source.inventory[: len(old.source.inventory)] == old.source.inventory
        )
        verified = len(old.index) if unchanged_prefix and old else 0
        digests = (
            list(old.digests) if unchanged_prefix and old else [_digest("", context_key.encode())]
        )
        contents = []
        structural_failure = source.scope_failure
        for entry in source.inventory[verified:]:
            revision = entry.revision
            try:
                content = Path(entry.path).read_bytes()
            except OSError as exc:
                structural_failure = ReplayFailure(
                    revision=revision,
                    created_at=_patch_failure_created_at(Path(entry.path)),
                    code="patch-read-failed",
                    message=str(exc),
                )
                break
            contents.append((revision, content))
            digests.append(_digest(digests[-1], str(revision).encode() + b"\0" + content))
        reusable = (
            old is not None
            and old.result.state.replay_status == "complete"
            and old.context_key == context_key
            and tuple(digests[: len(old.digests)]) == old.digests
        )
        if reusable and old and len(digests) == len(old.digests) and structural_failure is None:
            return replace(old, source=source, start=len(old.index))
        if source.metadata is not None and not reusable:
            assert main is not None
            base_result = self._prefix_result(main, source.metadata.base_head, use_store=use_store)
            initial = base_result.state
            inherited = main.index[: source.metadata.base_head.revision]
        elif reusable and old:
            initial, inherited = old.initial, old.inherited
        index = list(old.index) if reusable and old else []
        previous = old.result if reusable and old else base_result
        start = len(index)
        checkpoints = dict(old.checkpoints) if reusable and old else {}
        with _MEMORY_LOCK:
            states = OrderedDict(old.states) if reusable and old else OrderedDict()
            state_costs = dict(old.state_costs) if reusable and old else {}
        patches: list[Patch] = []
        for entry, (_, content) in zip(
            [] if source.scope_failure else source.inventory[start:],
            contents[start - verified :],
            strict=False,
        ):
            try:
                patches.append(self.manager._decode_persisted_patch(content.decode()))
            except ValueError as exc:
                structural_failure = ReplayFailure(
                    revision=entry.revision,
                    created_at=_patch_failure_created_at(Path(entry.path)),
                    code="patch-schema-invalid",
                    message=str(exc),
                )
                break
        for offset, (patch, entry) in enumerate(
            zip(patches, source.inventory[start : start + len(patches)], strict=True)
        ):
            revision = entry.revision
            expected = initial_head.revision + start + offset + 1
            if revision != expected or patch.revision != revision:
                structural_failure = ReplayFailure(
                    revision=expected,
                    created_at=patch.created_at,
                    code="patch-revision-discontinuous",
                    message=(
                        f"Graph history expected revision {expected}, but found "
                        f"{Path(entry.path).name} carrying revision {patch.revision}."
                    ),
                )
                patches = patches[:offset]
                break
        before_head = index[-1].after_head if index else initial_head
        failure = accepted_transition_head_chain_failure(
            patches, target=source.target, initial_transition_id=before_head.transition_id
        )
        if failure is not None:
            # A broken transition chain keeps the coherent prefix before it, read-only.
            patches = [patch for patch in patches if patch.revision < failure.revision]
            structural_failure = failure
        all_patches = [*previous.patches, *patches]
        stride = self.checkpoint_interval or limits.GRAPH_HISTORY_CHECKPOINT_INTERVAL
        stored_index = (
            self.store.load_step_segments(tuple(digests), stride)
            if use_store and self.store and not reusable and structural_failure is None
            else None
        )
        if stored_index is not None and (
            len(stored_index) != len(source.inventory)
            or any(step.patch != patch for step, patch in zip(stored_index, patches, strict=False))
        ):
            stored_index = None
        if stored_index is not None:
            # The store is an acceleration, never an alternative Patch inventory.
            if all(step.patch == patch for step, patch in zip(stored_index, patches, strict=True)):
                index = stored_index
                for item, patch in zip(index, patches, strict=True):
                    item.patch = patch
                last_checkpoint = initial_head.revision + len(index)
                last_checkpoint -= last_checkpoint % stride
                for revision in dict.fromkeys(
                    [
                        initial_head.revision + len(index),
                        *range(last_checkpoint, initial_head.revision, -stride),
                    ]
                ):
                    offset = revision - initial_head.revision
                    if offset == 0 or not self.checkpoint_interval:
                        continue
                    checkpoint = self.store.load_checkpoint(digests[offset]) if self.store else None
                    if checkpoint is not None and checkpoint.head == index[offset - 1].after_head:
                        previous = checkpoint.result
                        previous.patches = [
                            p for p in all_patches if p.revision <= previous.state.revision
                        ]
                        start = offset
                        before_head = checkpoint.head
                        break
                else:
                    start = 0
                patches = [step.patch for step in index[start:]]
        else:
            stored_index = None
        pending = list(old.pending) if reusable and old else []
        # Work in checkpoint intervals, releasing replay boundary dictionaries
        # once their compact semantic facts have been extracted.
        first = stride - previous.state.revision % stride
        chunks = [
            patches[:first],
            *(patches[i : i + stride] for i in range(first, len(patches), stride)),
        ]
        for chunk in chunks:
            if not chunk:
                continue
            accepted: dict[int, tuple[GraphState, GraphState]] = {}

            def observe(
                before: GraphState,
                patch: Patch,
                after: GraphState,
                boundaries: dict[int, tuple[GraphState, GraphState]] = accepted,
            ) -> None:
                boundaries[patch.revision] = (before, after)

            tail = materialize_patches_from_state(
                chunk,
                initial_state=previous.state,
                context=build_validation_context(source.manifest, initial, mode="replay"),
                accepted_patch_observer=observe if stored_index is None else None,
            )
            # A halted replay keeps its coherent prefix as a read-only generation.
            halted = tail.state.replay_status == "degraded"
            if halted:
                chunk = [patch for patch in chunk if patch.revision <= tail.state.revision]
            current = previous.state
            for patch in chunk:
                transition = (
                    patch.transition.transition_id
                    if patch.admission == "accepted" and patch.transition
                    else before_head.transition_id
                )
                after_head = GraphHeadRef(
                    target=source.target, revision=patch.revision, transition_id=transition
                )
                if stored_index is None:
                    if patch.admission == "accepted":
                        before, after = accepted[patch.revision]
                    else:
                        before = current
                        after = before.model_copy(
                            update={
                                "revision": patch.revision,
                                "validation_messages": [
                                    *before.validation_messages,
                                    *patch.admission_messages,
                                ],
                            }
                        )
                    delta = semantic_delta(before, after)
                    index.append(
                        IndexedStep(
                            before_head=before_head,
                            after_head=after_head,
                            patch=patch,
                            delta=delta,
                            changed_node_ids=frozenset(d.node_id for d in delta.nodes),
                            changed_edge_ids=frozenset(d.edge_id for d in delta.edges),
                            accepted=patch.admission == "accepted",
                            state_delta=capture_state_delta(before, after),
                        )
                    )
                    current = after
                before_head = after_head
            for _, after in list(accepted.values())[-limits.GRAPH_HISTORY_MEMORY_STATES :]:
                states[after.revision] = after
            while len(states) > limits.GRAPH_HISTORY_MEMORY_STATES:
                states.popitem(last=False)
            previous = MaterializationResult(
                tail.state,
                {**previous.reports, **tail.reports},
                [*previous.repository_descriptors, *tail.repository_descriptors],
                {**previous.processed_cursors, **tail.processed_cursors},
                [*previous.patches, *tail.patches],
            )
            if halted:
                break
            if tail.state.revision % stride == 0:
                checkpoints[tail.state.revision] = tail.state
                if self.checkpoint_interval:
                    pending.append(before_head)
        structural_failure = source.scope_failure or structural_failure
        if structural_failure is not None and previous.state.replay_status == "complete":
            previous = replace(
                previous,
                state=previous.state.model_copy(
                    update={"replay_status": "degraded", "replay_failure": structural_failure}
                ),
            )
        previous = replace(previous, patches=all_patches)
        return _Generation(
            source.target,
            context_key,
            tuple(digests),
            source.manifest,
            initial,
            initial_head,
            tuple(index),
            previous,
            source,
            inherited,
            checkpoints,
            states,
            state_costs,
            pending,
            len(old.index) if reusable and old else 0,
            saved=stored_index is not None,
            store_start=(old.store_start if not old.saved else len(old.index))
            if reusable and old
            else 0,
        )

    def _remember(self, built: dict[str, _Generation], serial: int) -> None:
        with _MEMORY_LOCK:
            if serial < self._published_serial:
                return
            self._published_serial = serial
            recount = False
            for target, generation in built.items():
                key = (id(self), target)
                previous = self._generations.get(target)
                if previous is not generation:
                    self._generations[target] = generation
                    if (
                        previous is not None
                        and generation.context_key == previous.context_key
                        and generation.start == len(previous.index)
                    ):
                        # Charge the new tail conservatively without walking its retained prefix.
                        self._retained_bytes += retained_size(generation.index[generation.start :])
                        self._retained_bytes += sum(
                            sys.getsizeof(value)
                            for value in generation.result.state.__dict__.values()
                        )
                    else:
                        recount = True
                _MEMORY[key] = weakref.ref(self)
                _MEMORY.move_to_end(key)
            if recount:
                # Count shared inherited prefixes once for their project owner.
                self._recount_memory()
            self._trim_memory()

    def _recount_memory(self) -> None:
        self._retained_bytes = (
            retained_size(tuple(self._generations.values())) if self._generations else 0
        ) + sum(size for _, _, size in self._answers.values())
        for generation in self._generations.values():
            generation.state_costs.clear()

    def _trim_memory(self) -> None:
        with _MEMORY_LOCK:
            owners = {}
            for key, reference in list(_MEMORY.items()):
                owner = reference()
                if owner is None:
                    del _MEMORY[key]
                else:
                    owners[id(owner)] = owner
            while _MEMORY:
                total = sum(owner._retained_bytes for owner in owners.values())
                if (
                    self._retained_bytes <= limits.GRAPH_HISTORY_MEMORY_PROJECT_BYTES
                    and total <= limits.GRAPH_HISTORY_MEMORY_PROCESS_BYTES
                ):
                    break
                victim = (
                    next(key for key in _MEMORY if key[0] == id(self))
                    if self._retained_bytes > limits.GRAPH_HISTORY_MEMORY_PROJECT_BYTES
                    else next(iter(_MEMORY))
                )
                owner = _MEMORY.pop(victim)()
                if owner is not None:
                    if victim[1] == "answers":
                        owner._answers.clear()
                    else:
                        owner._generations.pop(victim[1], None)
                    owner._recount_memory()

    def _build_sources(
        self, sources: dict[str, _Source], *, use_store: bool = True
    ) -> dict[str, _Generation]:
        main = self._build(sources["main"], None, use_store=use_store)
        return {
            "main": main,
            **{
                key: self._build(source, main, use_store=use_store)
                for key, source in sources.items()
                if key != "main"
            },
        }

    def _extends_verified(self, source: _Source) -> bool:
        with _MEMORY_LOCK:
            old = self._generations.get(source.target.key)
        return (
            old is not None
            and old.source.context_key == source.context_key
            and source.inventory[: len(old.index)] == old.source.inventory
            and len(source.inventory) - len(old.index)
            <= (self.checkpoint_interval or limits.GRAPH_HISTORY_CHECKPOINT_INTERVAL)
        )

    def _pin(
        self,
        targets: list[GraphTargetRef],
        *,
        refresh: bool = True,
        use_store: bool = True,
        pending_patch_paths: list[Path] | None = None,
        refresh_max_age_seconds: float = REMOTE_STATE_RECONCILE_WINDOW_SECONDS,
    ) -> dict[str, _Generation]:
        self._resolved.generations = None
        use_store = use_store and not pending_patch_paths
        while True:
            built: dict[str, _Generation] | None = None
            serial: int | None = None
            with self.manager.workspace.snapshot_lock:
                sources = self._capture(
                    targets,
                    refresh=refresh,
                    pending_patch_paths=pending_patch_paths,
                    refresh_max_age_seconds=refresh_max_age_seconds,
                )
                incremental = all(self._extends_verified(source) for source in sources.values())
                if incremental:
                    # A bounded tail can stay inside the canonical fence. No second inventory
                    # scan is needed: neither append nor mirror refresh can race its replay.
                    built = self._build_sources(sources, use_store=False)
                    self._verified_serial += 1
                    serial = self._verified_serial
            if not incremental:
                try:
                    built = self._build_sources(sources, use_store=use_store)
                except (OSError, ValueError):
                    with self.manager.workspace.snapshot_lock:
                        if sources != self._capture(
                            targets, refresh=False, pending_patch_paths=pending_patch_paths
                        ):
                            continue
                    raise
                with self.manager.workspace.snapshot_lock:
                    if sources != self._capture(
                        targets, refresh=False, pending_patch_paths=pending_patch_paths
                    ):
                        continue
                    for key, source in sources.items():
                        if source.metadata is not None:
                            self._branch_identities[key] = source.metadata.model_dump_json(
                                exclude={"head"}
                            )
                    self._verified_serial += 1
                    serial = self._verified_serial
            assert built is not None and serial is not None
            if pending_patch_paths:
                return built
            self._remember(built, serial)
            # Optional cache writes happen only after canonical confirmation,
            # outside its lock; append never depends on checkpoint publication.
            if use_store and self.store:
                for generation in built.values():
                    if generation.result.state.replay_status == "complete":
                        for head in generation.pending:
                            result = self._prefix_result(
                                generation, head, state=generation.checkpoints[head.revision]
                            )
                            self.store.save_checkpoint(generation.prefix(head), head, result)
                        if not generation.saved:
                            self.store.save_step_segments(
                                generation.digests,
                                list(generation.index),
                                self.checkpoint_interval
                                or limits.GRAPH_HISTORY_CHECKPOINT_INTERVAL,
                                start=generation.store_start,
                            )
                        generation.pending.clear()
                        generation.saved = True
            return built

    def _state(
        self, generation: _Generation, head: GraphHeadRef, *, use_store: bool = True
    ) -> GraphState:
        generation.prefix(head)
        if head.revision == generation.result.state.revision:
            return generation.result.state
        if head.revision == generation.initial_head.revision:
            return generation.initial
        with _MEMORY_LOCK:
            if head.revision in generation.states:
                generation.states.move_to_end(head.revision)
                return generation.states[head.revision]
            state = max(
                (
                    value
                    for value in (*generation.checkpoints.values(), *generation.states.values())
                    if value.revision <= head.revision
                ),
                key=lambda value: value.revision,
                default=generation.initial,
            )
        stride = self.checkpoint_interval or limits.GRAPH_HISTORY_CHECKPOINT_INTERVAL
        if use_store and self.store and self.checkpoint_interval:
            revision = head.revision - head.revision % stride
            while revision > state.revision:
                offset = revision - generation.initial_head.revision
                checkpoint = self.store.load_checkpoint(generation.digests[offset])
                if checkpoint is not None and checkpoint.head == generation.head(revision):
                    state = checkpoint.result.state
                    break
                revision -= stride
        start = state.revision - generation.initial_head.revision
        stop = head.revision - generation.initial_head.revision
        for item in generation.index[start:stop]:
            state = apply_state_delta(state, item.state_delta)
        size = retained_size(state)
        with _MEMORY_LOCK:
            generation.states[head.revision] = state
            retained = self._generations.get(generation.target.key) is generation
            if retained:
                previous_cost = generation.state_costs.get(head.revision, 0)
                generation.state_costs[head.revision] = size
                self._retained_bytes += size - previous_cost
            while len(generation.states) > limits.GRAPH_HISTORY_MEMORY_STATES:
                revision, _ = generation.states.popitem(last=False)
                removed = generation.state_costs.pop(revision, 0)
                if retained:
                    self._retained_bytes -= removed
            self._trim_memory()
        return state

    def resolve(self, spec: HeadSpec, *, default_target: GraphTargetRef) -> GraphHeadRef:
        target = spec.target or default_target
        generations = self._pin([target])
        generation = generations[target.key]
        revision = generation.result.state.revision if spec.revision == "head" else spec.revision
        head = generation.head(revision)
        generation.prefix(head)
        self._resolved.generations = {key: weakref.ref(value) for key, value in generations.items()}
        self._resolved.head = head
        return head

    def state_at(self, head: GraphHeadRef) -> GraphState:
        generation = self._query_generations([head], None)[head.target.key]
        return copy_state(self._state(generation, head))

    @staticmethod
    def _index(generation: _Generation, head: GraphHeadRef, inherited: bool) -> list[IndexedStep]:
        generation.prefix(head)
        prefix = (
            [r.model_copy(update={"inherited": True}) for r in generation.inherited]
            if inherited
            else []
        )
        return prefix + [r for r in generation.index if r.after_head.revision <= head.revision]

    def _steps(
        self,
        generation: _Generation,
        head: GraphHeadRef,
        *,
        from_revision: int = 1,
        use_store: bool = True,
    ) -> Iterator[StepRecord]:
        generation.prefix(head)
        start = max(generation.initial_head.revision, from_revision - 1)
        if start >= head.revision:
            return
        state = self._state(generation, generation.head(start), use_store=use_store)
        for item in generation.index[
            start - generation.initial_head.revision : head.revision
            - generation.initial_head.revision
        ]:
            after = apply_state_delta(state, item.state_delta)
            yield StepRecord(
                item.before_head,
                item.after_head,
                state,
                after,
                item.patch,
                item.delta,
                item.changed_node_ids,
                item.changed_edge_ids,
                item.accepted,
            )
            state = after

    def indexed_steps(
        self, head: GraphHeadRef, *, include_inherited: bool = False
    ) -> list[IndexedStep]:
        generation = self._query_generations([head], None)[head.target.key]
        return self._index(generation, head, include_inherited)

    def steps(self, head: GraphHeadRef, *, include_inherited: bool = False) -> list[StepRecord]:
        generations = self._query_generations([head], None)
        generation = generations[head.target.key]
        inherited = []
        if include_inherited and generation.inherited:
            base = generation.inherited[-1].after_head
            inherited = [replace(s, inherited=True) for s in self._steps(generations["main"], base)]
        return inherited + list(self._steps(generation, head))

    def snapshot(
        self,
        target: GraphTargetRef,
        *,
        refresh: bool = True,
        use_store: bool = True,
        refresh_max_age_seconds: float = REMOTE_STATE_RECONCILE_WINDOW_SECONDS,
    ) -> _Generation:
        """Capture metadata, base, index and head as one verified generation."""
        return self._pin(
            [target],
            refresh=refresh,
            use_store=use_store,
            refresh_max_age_seconds=refresh_max_age_seconds,
        )[target.key]

    def projection(
        self, target: GraphTargetRef, *, from_revision: int = 1, to_revision: int | None = None
    ) -> tuple[MaterializationResult, tuple[IndexedStep, ...]]:
        generation = self.snapshot(target)
        base = generation.initial_head.revision
        stop = len(generation.index) if to_revision is None else max(0, to_revision - base)
        return generation.result, generation.index[max(0, from_revision - base - 1) : stop]

    def _prefix_result(
        self,
        generation: _Generation,
        head: GraphHeadRef,
        *,
        state: GraphState | None = None,
        use_store: bool = True,
    ) -> MaterializationResult:
        if state is None:
            state = self._state(generation, head, use_store=use_store)
        if state.replay_failure is not None and state.replay_failure.revision > head.revision:
            state = state.model_copy(update={"replay_status": "complete", "replay_failure": None})
        patches = [p for p in generation.result.patches if p.revision <= head.revision]
        accepted = [p for p in patches if p.admission == "accepted"]
        return MaterializationResult(
            state,
            {r: v for r, v in generation.result.reports.items() if r <= head.revision},
            [
                op.repository.model_dump(mode="python", exclude_unset=True)
                for p in accepted
                for op in p.ops
                if isinstance(op, SetProjectTruthScopeOperation) and op.repository is not None
            ],
            {k: v for p in accepted for k, v in p.processed_cursors.items()},
            patches,
        )

    def materialization(
        self,
        target: GraphTargetRef,
        *,
        refresh: bool = True,
        refresh_max_age_seconds: float = REMOTE_STATE_RECONCILE_WINDOW_SECONDS,
        pending_patch_paths: list[Path] | None = None,
        accepted_patch_observer: AcceptedPatchObserver | None = None,
    ) -> MaterializationResult:
        """Write-side projection: optional cache I/O never delays canonical publication.

        A pending tail uses the same replay core but is not retained or cached until
        it appears in canonical inventory. Callers own the workspace publication fence.
        """
        generations = self._pin(
            [target],
            refresh=refresh,
            refresh_max_age_seconds=refresh_max_age_seconds,
            use_store=False,
            pending_patch_paths=pending_patch_paths,
        )
        generation = generations[target.key]
        if accepted_patch_observer is not None:
            boundaries = []
            if generation.inherited:
                boundaries.append((generations["main"], generation.inherited[-1].after_head))
            if generation.index:
                boundaries.append((generation, generation.head(generation.result.state.revision)))
            for source, head in boundaries:
                for step in self._steps(source, head, use_store=False):
                    if step.accepted:
                        accepted_patch_observer(step.before_state, step.patch, step.after_state)
        return generation.result

    def snapshots(
        self,
        targets: list[GraphTargetRef],
        *,
        refresh: bool = True,
        refresh_max_age_seconds: float = REMOTE_STATE_RECONCILE_WINDOW_SECONDS,
    ) -> dict[str, _Generation]:
        return self._pin(targets, refresh=refresh, refresh_max_age_seconds=refresh_max_age_seconds)

    def _query_generations(
        self, heads: list[GraphHeadRef], cursor: str | None
    ) -> dict[str, _Generation]:
        resolved = getattr(self._resolved, "generations", None)
        self._resolved.generations = None
        if resolved is not None and getattr(self._resolved, "head", None) in heads:
            pinned = {key: reference() for key, reference in resolved.items()}
            if all(pinned.get(head.target.key) is not None for head in heads):
                generations = {key: value for key, value in pinned.items() if value is not None}
                for head in heads:
                    generations[head.target.key].prefix(head)
                return generations
        if cursor is not None:
            # A continuation reads its already verified historical snapshot;
            # it does not resolve a moving head again. Eviction re-proves inputs.
            with self.manager.workspace.snapshot_lock, _MEMORY_LOCK:
                generations = {h.target.key: self._generations.get(h.target.key) for h in heads}
                if all(g is not None for g in generations.values()):
                    pinned = {key: g for key, g in generations.items() if g is not None}
                    try:
                        for h in heads:
                            pinned[h.target.key].prefix(h)
                    except ValueError:
                        pass
                    else:
                        return pinned
        return self._pin([h.target for h in heads])

    def _cached_answer(self, cursor: str, request: str) -> DiffAnswer | LogAnswer | None:
        from rcp.history.pages import cursor_key

        self._resolved.generations = None
        key = cursor_key(cursor)
        with _MEMORY_LOCK:
            cached = self._answers.get(key)
            if cached is None:
                return None
            if cached[0] != request:
                raise ValueError("history cursor does not match this query")
            self._answers.move_to_end(key)
            return cached[1]

    def _remember_answer(self, request: str, answer: DiffAnswer | LogAnswer) -> None:
        size = answer.retained_bytes
        with _MEMORY_LOCK:
            previous = self._answers.pop(answer.key, None)
            if previous is not None:
                self._retained_bytes -= previous[2]
            self._answers[answer.key] = request, answer, size
            self._retained_bytes += size
            while self._answers and (len(self._answers) > limits.GRAPH_HISTORY_MEMORY_ANSWERS):
                _, (_, _, removed) = self._answers.popitem(last=False)
                self._retained_bytes -= removed
            _MEMORY[(id(self), "answers")] = weakref.ref(self)
            _MEMORY.move_to_end((id(self), "answers"))
            self._trim_memory()

    def diff(
        self,
        from_head: GraphHeadRef,
        to_head: GraphHeadRef,
        *,
        ids: frozenset[str] = frozenset(),
        cursor: str | None = None,
    ) -> GraphDiffPage:
        from rcp.history.pages import compute_diff, diff_key

        request = diff_key(from_head, to_head, ids, "")
        if cursor is not None:
            cached = self._cached_answer(cursor, request)
            if isinstance(cached, DiffAnswer):
                return cached.page(cursor)
        generations = self._query_generations([from_head, to_head], cursor)
        before, after = generations[from_head.target.key], generations[to_head.target.key]
        identity = before.prefix(from_head) + after.prefix(to_head)
        key = diff_key(from_head, to_head, ids, identity)
        cached = self._answers.get(key)
        if cached is not None and isinstance(cached[1], DiffAnswer):
            return cached[1].page(cursor)
        if before is after:
            low, high = sorted((from_head.revision, to_head.revision))
            base = before.initial_head.revision
            changed_steps = list(before.index[low - base : high - base])
            from_steps, to_steps = (
                (changed_steps, [])
                if from_head.revision > to_head.revision
                else ([], changed_steps)
            )
        else:
            from_steps = self._index(before, from_head, True)
            to_steps = self._index(after, to_head, True)
        answer = compute_diff(
            from_head,
            to_head,
            self._state(before, from_head),
            self._state(after, to_head),
            from_steps,
            to_steps,
            ids=ids,
            identity=identity,
        )
        self._remember_answer(request, answer)
        return answer.page(cursor)

    def log(
        self, entity_id: str, at_head: GraphHeadRef, *, cursor: str | None = None
    ) -> GraphLogPage:
        from rcp.history.pages import compute_log, log_key

        request = log_key(entity_id, at_head, "")
        if cursor is not None:
            cached = self._cached_answer(cursor, request)
            if isinstance(cached, LogAnswer):
                return cached.page(cursor)
        generation = self._query_generations([at_head], cursor)[at_head.target.key]
        identity = generation.prefix(at_head)
        key = log_key(entity_id, at_head, identity)
        cached = self._answers.get(key)
        if cached is not None and isinstance(cached[1], LogAnswer):
            return cached[1].page(cursor)
        answer = compute_log(
            entity_id,
            at_head,
            self._index(generation, at_head, True),
            identity=identity,
        )
        self._remember_answer(request, answer)
        return answer.page(cursor)
