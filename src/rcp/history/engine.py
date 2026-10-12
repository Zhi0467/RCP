"""Verified, project-owned replay generations for graph history reads."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from rcp import limits
from rcp.core.materialize import MaterializationResult, materialize_patches_from_state
from rcp.core.models import GraphBranchMetadata, GraphState, Patch
from rcp.core.operations import SetProjectTruthScopeOperation
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.core.transitions import accepted_transition_head_chain_failure
from rcp.core.validation.settings import build_validation_context
from rcp.history.delta import semantic_delta
from rcp.history.memory_size import retained_size
from rcp.history.queries import GraphDiffPage, GraphLogPage, HeadSpec, StepRecord
from rcp.history.state_delta import apply_state_delta, capture_state_delta
from rcp.history.store import GraphHistoryStore, IndexedStep
from rcp.limits import REMOTE_STATE_RECONCILE_WINDOW_SECONDS
from rcp.transport import StateUnavailable

if TYPE_CHECKING:
    from rcp.config import Manifest
    from rcp.history.manager import HistoryManager

_VERSION = "graph-history-2"
_LOGGER = logging.getLogger(__name__)
_MEMORY_LOCK = threading.RLock()
_MEMORY: OrderedDict[tuple[int, str], weakref.ReferenceType[ProjectHistoryEngine]] = OrderedDict()


def _digest(previous: str, content: bytes) -> str:
    return hashlib.sha256(previous.encode() + len(content).to_bytes(8, "big") + content).hexdigest()


@dataclass(frozen=True)
class _Source:
    target: GraphTargetRef
    manifest: Manifest
    context_key: str
    scope: list[str]
    contents: tuple[tuple[int, bytes], ...]
    metadata: GraphBranchMetadata | None = None


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
    inherited: tuple[IndexedStep, ...] = ()
    pending: list[GraphHeadRef] = field(default_factory=list)
    pending_start: GraphState | None = None
    saved: bool = False

    def head(self, revision: int) -> GraphHeadRef:
        offset = revision - self.initial_head.revision
        if offset == 0:
            return self.initial_head
        if 0 < offset <= len(self.index):
            return self.index[offset - 1].after_head
        raise ValueError("history revision is absent or precedes the branch base")

    def prefix(self, head: GraphHeadRef) -> str:
        if self.head(head.revision) != head:
            raise ValueError("history head transition identity does not match")
        return self.digests[head.revision - self.initial_head.revision]


class ProjectHistoryEngine:
    """One owner shared by main and every short-lived branch service.

    Generations retain compact step indexes and endpoint states. Historical
    state dictionaries are reconstructed only for requested intervals.
    """

    def __init__(self, manager: HistoryManager) -> None:
        self.manager = manager
        self._generations: dict[str, _Generation] = {}
        self.store: GraphHistoryStore | None = None
        self.checkpoint_interval = limits.GRAPH_HISTORY_CHECKPOINT_INTERVAL
        self._branch_identities: dict[str, str] = {}
        self._verified_serial = 0
        self._published_serial = 0
        self._retained_bytes = 0

    def configure_store(self, data_dir: Path, project_id: str) -> None:
        with self.manager.workspace.snapshot_lock:
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
        refresh_max_age_seconds: float = REMOTE_STATE_RECONCILE_WINDOW_SECONDS,
    ) -> dict[str, _Source]:
        from rcp.history.branches import (
            BranchHistoryManager,
            _require_regular_file,
            _safe_branch_root,
            canonical_branch_id,
        )

        parent = self.manager
        if refresh and not parent.workspace.refresh_if_stale(refresh_max_age_seconds):
            raise StateUnavailable("canonical state refresh did not confirm a current snapshot")
        parent._reload_manifest()
        manifest = parent.manifest.model_copy(deep=True)
        main_paths = parent._patch_paths()
        scope_path = parent.root / "scope-base.json"
        if scope_path.exists():
            scope = json.loads(scope_path.read_bytes())["truth_scope"]
            if not isinstance(scope, list) or any(not isinstance(item, str) for item in scope):
                raise ValueError("invalid canonical scope provenance")
        elif main_paths:
            raise ValueError("canonical scope provenance is missing")
        else:
            scope = list(manifest.project.truth_scope)
        context = build_validation_context(
            manifest, GraphState(project_truth_scope=scope), mode="replay"
        )
        key = json.dumps(
            [
                _VERSION,
                parent.project_id,
                str(parent.root.resolve()),
                scope,
                sorted(context.repository_aliases or ()),
                sorted(context.machine_aliases or ()),
                sorted(context.default_run_truth_scope or ()),
                context.state_repository,
            ],
            sort_keys=True,
        )

        def contents(paths: list[Path]) -> tuple[tuple[int, bytes], ...]:
            retained = []
            for path in paths:
                _require_regular_file(path, "retained Patch")
                retained.append((int(path.stem), path.read_bytes()))
            return tuple(retained)

        sources = {
            "main": _Source(
                GraphTargetRef(),
                manifest,
                key,
                scope,
                contents(main_paths),
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
            branch = BranchHistoryManager(parent, metadata)
            sources[target.key] = _Source(
                target,
                manifest,
                key,
                scope,
                contents(branch._patch_paths()),
                metadata,
            )
        return sources

    def _build(self, source: _Source, main: _Generation | None) -> _Generation:
        initial = GraphState(project_truth_scope=source.scope)
        initial_head = GraphHeadRef(target=source.target, revision=0)
        inherited: tuple[IndexedStep, ...] = ()
        context_key = source.context_key
        base_result = MaterializationResult(state=initial)
        if source.metadata is not None:
            assert main is not None
            from rcp.history.branches import _require_parent_identity

            _require_parent_identity(
                self.manager,
                source.metadata,
                allow_historical_home=True,
                materialization=main.result,
            )
            identity = source.metadata.model_dump_json(exclude={"head"})
            known = self._branch_identities.get(source.target.key)
            if known is not None and known != identity:
                raise ValueError("graph branch immutable metadata changed")
            base = source.metadata.base_head
            base_result = self._prefix_result(main, base)
            initial = base_result.state
            initial_head = base.model_copy(update={"target": source.target})
            inherited = tuple(r for r in main.index if r.after_head.revision <= base.revision)
            context_key += source.metadata.model_dump_json(exclude={"head"}) + main.prefix(base)
        digests = [_digest("", context_key.encode())]
        for revision, content in source.contents:
            digests.append(_digest(digests[-1], str(revision).encode() + b"\0" + content))
        with _MEMORY_LOCK:
            old = self._generations.get(source.target.key)
        if old is not None and old.context_key == context_key and old.digests == tuple(digests):
            return old
        reusable = (
            old is not None
            and old.context_key == context_key
            and len(old.digests) <= len(digests)
            and tuple(digests[: len(old.digests)]) == old.digests
        )
        index = list(old.index) if reusable and old else []
        previous = old.result if reusable and old else base_result
        start = len(index)
        patches = [
            self.manager._decode_persisted_patch(content.decode())
            for _, content in source.contents[start:]
        ]
        for offset, (patch, (revision, _)) in enumerate(
            zip(patches, source.contents[start:], strict=True)
        ):
            if revision != initial_head.revision + start + offset + 1 or patch.revision != revision:
                raise ValueError("retained Patch revision sequence is invalid")
        before_head = index[-1].after_head if index else initial_head
        failure = accepted_transition_head_chain_failure(
            patches, target=source.target, initial_transition_id=before_head.transition_id
        )
        if failure is not None:
            raise ValueError(f"invalid retained transition chain: {failure.code}")
        all_patches = [*previous.patches, *patches]
        stored_index = self.store.load_steps(digests[-1]) if self.store and not reusable else None
        if stored_index is not None and (
            len(stored_index) != len(source.contents)
            or any(step.patch != patch for step, patch in zip(stored_index, patches, strict=False))
        ):
            stored_index = None
        if stored_index is not None:
            # The store is an acceleration, never an alternative Patch inventory.
            if all(step.patch == patch for step, patch in zip(stored_index, patches, strict=True)):
                index = stored_index
                for offset in range(len(index) if self.checkpoint_interval else 0, 0, -1):
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
        pending: list[GraphHeadRef] = []
        pending_start = previous.state
        # Work in checkpoint intervals, releasing replay boundary dictionaries
        # once their compact semantic facts have been extracted.
        stride = self.checkpoint_interval or limits.GRAPH_HISTORY_CHECKPOINT_INTERVAL
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
            self.manager.require_writable(tail.state)
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
            previous = MaterializationResult(
                tail.state,
                {**previous.reports, **tail.reports},
                [*previous.repository_descriptors, *tail.repository_descriptors],
                {**previous.processed_cursors, **tail.processed_cursors},
                [*previous.patches, *tail.patches],
            )
            if (
                self.store
                and self.checkpoint_interval
                and tail.state.revision % self.checkpoint_interval == 0
            ):
                pending.append(before_head)
        return _Generation(
            source.target,
            context_key,
            tuple(digests),
            source.manifest,
            initial,
            initial_head,
            tuple(index),
            previous,
            inherited,
            pending,
            pending_start if pending else None,
            saved=stored_index is not None,
        )

    def _remember(self, built: dict[str, _Generation], serial: int) -> None:
        with _MEMORY_LOCK:
            if serial < self._published_serial:
                return
            self._published_serial = serial
            changed = False
            for target, generation in built.items():
                key = (id(self), target)
                if self._generations.get(target) is not generation:
                    self._generations[target] = generation
                    changed = True
                _MEMORY[key] = weakref.ref(self)
                _MEMORY.move_to_end(key)
            if changed:
                # Count shared inherited prefixes once for their project owner.
                self._retained_bytes = retained_size(tuple(self._generations.values()))
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
                    owner._generations.pop(victim[1], None)
                    owner._retained_bytes = (
                        retained_size(tuple(owner._generations.values())) if owner._generations else 0
                    )

    def _pin(
        self,
        targets: list[GraphTargetRef],
        *,
        refresh: bool = True,
        refresh_max_age_seconds: float = REMOTE_STATE_RECONCILE_WINDOW_SECONDS,
    ) -> dict[str, _Generation]:
        # Copy canonical bytes under the workspace fence; replay never holds it.
        while True:
            with self.manager.workspace.snapshot_lock:
                sources = self._capture(
                    targets, refresh=refresh, refresh_max_age_seconds=refresh_max_age_seconds
                )
            main = self._build(sources["main"], None)
            built = {"main": main}
            for key, source in sources.items():
                if key != "main":
                    built[key] = self._build(source, main)
            with self.manager.workspace.snapshot_lock:
                if sources != self._capture(targets, refresh=False):
                    continue
                for key, source in sources.items():
                    if source.metadata is not None:
                        self._branch_identities[key] = source.metadata.model_dump_json(
                            exclude={"head"}
                        )
                self._verified_serial += 1
                serial = self._verified_serial
            self._remember(built, serial)
            # Optional cache writes happen only after canonical confirmation,
            # outside its lock; append never depends on checkpoint publication.
            if self.store:
                for generation in built.values():
                    if not generation.saved:
                        state = generation.pending_start
                        pending = {head.revision: head for head in generation.pending}
                        if state is not None:
                            for step in generation.index:
                                if step.after_head.revision <= state.revision:
                                    continue
                                state = apply_state_delta(state, step.state_delta)
                                checkpoint_head = pending.get(state.revision)
                                if checkpoint_head is not None:
                                    result = self._prefix_result(
                                        generation, checkpoint_head, state=state
                                    )
                                    self.store.save_checkpoint(
                                        generation.prefix(checkpoint_head), checkpoint_head, result
                                    )
                        self.store.save_steps(generation.digests[-1], list(generation.index))
                        generation.pending.clear()
                        generation.pending_start = None
                        generation.saved = True
            return built

    def _state(self, generation: _Generation, head: GraphHeadRef) -> GraphState:
        generation.prefix(head)
        if head.revision == generation.initial_head.revision:
            return generation.initial
        if head.revision == generation.result.state.revision:
            return generation.result.state
        state = generation.initial
        if self.store and self.checkpoint_interval:
            for offset in range(head.revision - generation.initial_head.revision, 0, -1):
                checkpoint = self.store.load_checkpoint(generation.digests[offset])
                if checkpoint is not None and checkpoint.head == generation.head(
                    offset + generation.initial_head.revision
                ):
                    state = checkpoint.result.state
                    break
        for item in generation.index:
            if state.revision < item.after_head.revision <= head.revision:
                state = apply_state_delta(state, item.state_delta)
        return state

    def resolve(self, spec: HeadSpec, *, default_target: GraphTargetRef) -> GraphHeadRef:
        target = spec.target or default_target
        generation = self._pin([target])[target.key]
        revision = generation.result.state.revision if spec.revision == "head" else spec.revision
        return generation.head(revision)

    def state_at(self, head: GraphHeadRef) -> GraphState:
        generation = self._pin([head.target])[head.target.key]
        return self._state(generation, head).model_copy(deep=True)

    @staticmethod
    def _index(generation: _Generation, head: GraphHeadRef, inherited: bool) -> list[IndexedStep]:
        generation.prefix(head)
        prefix = (
            [r.model_copy(update={"inherited": True}) for r in generation.inherited]
            if inherited
            else []
        )
        return prefix + [r for r in generation.index if r.after_head.revision <= head.revision]

    def _steps(self, generation: _Generation, head: GraphHeadRef) -> list[StepRecord]:
        generation.prefix(head)
        state = generation.initial
        records = []
        for item in self._index(generation, head, False):
            after = apply_state_delta(state, item.state_delta)
            records.append(
                StepRecord(
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
            )
            state = after
        return records

    def steps(self, head: GraphHeadRef, *, include_inherited: bool = False) -> list[StepRecord]:
        from dataclasses import replace

        generations = self._pin([head.target])
        generation = generations[head.target.key]
        inherited = []
        if include_inherited and generation.inherited:
            base = generation.inherited[-1].after_head
            inherited = [replace(s, inherited=True) for s in self._steps(generations["main"], base)]
        return inherited + self._steps(generation, head)

    def projection(self, target: GraphTargetRef) -> tuple[MaterializationResult, list[StepRecord]]:
        generation = self._pin([target])[target.key]
        return generation.result, self._steps(
            generation, generation.head(generation.result.state.revision)
        )

    def _prefix_result(
        self, generation: _Generation, head: GraphHeadRef, *, state: GraphState | None = None
    ) -> MaterializationResult:
        patches = [p for p in generation.result.patches if p.revision <= head.revision]
        accepted = [p for p in patches if p.admission == "accepted"]
        return MaterializationResult(
            state if state is not None else self._state(generation, head),
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
    ) -> MaterializationResult:
        return self._pin(
            [target], refresh=refresh, refresh_max_age_seconds=refresh_max_age_seconds
        )[target.key].result

    def projections(
        self,
        targets: list[GraphTargetRef],
        *,
        refresh: bool = True,
        refresh_max_age_seconds: float = REMOTE_STATE_RECONCILE_WINDOW_SECONDS,
    ) -> dict[str, tuple[MaterializationResult, list[StepRecord]]]:
        generations = self._pin(
            targets, refresh=refresh, refresh_max_age_seconds=refresh_max_age_seconds
        )
        return {
            key: (g.result, self._steps(g, g.head(g.result.state.revision)))
            for key, g in generations.items()
        }

    def _query_generations(
        self, heads: list[GraphHeadRef], cursor: str | None
    ) -> dict[str, _Generation]:
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

    def diff(
        self,
        from_head: GraphHeadRef,
        to_head: GraphHeadRef,
        *,
        ids: frozenset[str] = frozenset(),
        cursor: str | None = None,
    ) -> GraphDiffPage:
        from rcp.history.pages import diff_page

        generations = self._query_generations([from_head, to_head], cursor)
        before, after = generations[from_head.target.key], generations[to_head.target.key]
        return diff_page(
            from_head,
            to_head,
            self._state(before, from_head),
            self._state(after, to_head),
            self._index(before, from_head, True),
            self._index(after, to_head, True),
            ids=ids,
            cursor=cursor,
            identity=before.prefix(from_head) + after.prefix(to_head),
        )

    def log(
        self, entity_id: str, at_head: GraphHeadRef, *, cursor: str | None = None
    ) -> GraphLogPage:
        from rcp.history.pages import log_page

        generation = self._query_generations([at_head], cursor)[at_head.target.key]
        return log_page(
            entity_id,
            at_head,
            self._index(generation, at_head, True),
            cursor=cursor,
            identity=generation.prefix(at_head),
        )
