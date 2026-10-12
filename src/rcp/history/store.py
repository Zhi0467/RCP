"""Optional, content-addressed history checkpoints and changed-id indexes."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidatorFunctionWrapHandler, field_validator

from rcp.core.materialize import MaterializationResult
from rcp.core.models import GraphState, Patch, ValidationMessage
from rcp.core.operations import adapt_persisted_graph_state_document, adapt_persisted_patch_document
from rcp.core.transition_models import GraphHeadRef
from rcp.core.validation.report import ValidationReport
from rcp.history.delta import SemanticDelta
from rcp.history.queries import StepRecord
from rcp.history.state_delta import StateDelta, capture_state_delta, restore_edge_layer
from rcp.limits import (
    GRAPH_HISTORY_STORE_MAX_BYTES,
    GRAPH_HISTORY_STORE_MAX_COUNT,
    GRAPH_HISTORY_STORE_TTL_SECONDS,
)
from rcp.sources.cache import CacheLimits, RebuildableCache, project_cache_roots

_LOGGER = logging.getLogger(__name__)
_DIGEST = re.compile(r"[0-9a-f]{64}")


class IndexedStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    before_head: GraphHeadRef
    after_head: GraphHeadRef
    patch: Patch
    delta: SemanticDelta
    changed_node_ids: frozenset[str]
    changed_edge_ids: frozenset[str]
    accepted: bool
    inherited: bool = False
    state_delta: StateDelta

    @field_validator("patch", mode="before")
    @classmethod
    def adapt_patch(cls, value: object) -> object:
        return adapt_persisted_patch_document(value) if isinstance(value, dict) else value

    @field_validator("delta", mode="wrap")
    @classmethod
    def restore_historical_delta(
        cls, value: object, handler: ValidatorFunctionWrapHandler
    ) -> SemanticDelta:
        if isinstance(value, dict):
            value = dict(value)
            proposals = []
            raw_proposals = value.get("proposals", [])
            if not isinstance(raw_proposals, list):
                raise ValueError("history proposal deltas must be a list")
            for change in raw_proposals:
                if not isinstance(change, dict):
                    raise ValueError("history proposal delta must be an object")
                change = dict(change)
                for side in ("before", "after"):
                    if isinstance(change.get(side), dict):
                        adapted = adapt_persisted_graph_state_document(
                            {
                                "proposals": {"snapshot": change[side]},
                            }
                        )
                        change[side] = adapted["proposals"]["snapshot"]
                proposals.append(change)
            value["proposals"] = proposals
        delta: SemanticDelta = handler(value)
        if isinstance(value, dict):
            for edge, document in zip(delta.edges, value.get("edges", []), strict=True):
                for side in ("before", "after"):
                    snapshot = getattr(edge, side)
                    if snapshot is not None:
                        setattr(edge, side, restore_edge_layer(snapshot, document.get(side)))
        return delta

    @classmethod
    def from_record(cls, record: StepRecord) -> IndexedStep:
        return cls(
            **{name: getattr(record, name) for name in cls.model_fields if name != "state_delta"},
            state_delta=capture_state_delta(record.before_state, record.after_state),
        )


@dataclass(frozen=True)
class Checkpoint:
    head: GraphHeadRef
    result: MaterializationResult


class _CheckpointPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    head: GraphHeadRef
    state: GraphState
    reports: dict[int, list[ValidationMessage]]
    repository_descriptors: list[dict[str, str]]
    processed_cursors: dict[str, str]

    @field_validator("state", mode="wrap")
    @classmethod
    def preserve_edge_layers(
        cls, value: object, handler: ValidatorFunctionWrapHandler
    ) -> GraphState:
        state: GraphState = handler(value)
        if isinstance(value, dict):
            documents = value.get("edges", {})
            return state.model_copy(
                update={
                    "edges": {
                        key: restore_edge_layer(edge, documents[key])
                        for key, edge in state.edges.items()
                    }
                }
            )
        return state


class _StepsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[IndexedStep] = Field(default_factory=list)


class _Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prefix_digest: str
    checksum: str
    payload: str


def graph_history_cache_root(data_dir: Path, project_id: str) -> Path:
    return project_cache_roots(data_dir, project_id)[0].parent / "graph-history"


class GraphHistoryStore:
    """A disposable cache; failures are reported and never escape to a writer."""

    def __init__(self, data_dir: Path, project_id: str) -> None:
        self.root = graph_history_cache_root(data_dir, project_id)
        self.cache = RebuildableCache(
            self.root,
            CacheLimits(
                ttl_seconds=GRAPH_HISTORY_STORE_TTL_SECONDS,
                max_count=GRAPH_HISTORY_STORE_MAX_COUNT,
                max_bytes=GRAPH_HISTORY_STORE_MAX_BYTES,
            ),
            layout="files",
        )

    def load_checkpoint(self, prefix_digest: str) -> Checkpoint | None:
        payload = self._load(prefix_digest, "checkpoint")
        if payload is None:
            return None
        try:
            stored = _CheckpointPayload.model_validate_json(payload)
            if (
                stored.head.revision != stored.state.revision
                or stored.state.replay_status != "complete"
            ):
                raise ValueError("checkpoint state does not match its receipt head")
            reports: dict[int, ValidationReport] = {}
            for revision, messages in stored.reports.items():
                report = ValidationReport()
                report.messages = messages
                reports[revision] = report
            return Checkpoint(
                stored.head,
                MaterializationResult(
                    stored.state,
                    reports,
                    stored.repository_descriptors,
                    stored.processed_cursors,
                ),
            )
        except (ValueError, TypeError, KeyError):
            _LOGGER.warning("Invalid graph history checkpoint; rebuilding", exc_info=True)
            return None

    def save_checkpoint(
        self,
        prefix_digest: str,
        head: GraphHeadRef,
        result: MaterializationResult,
    ) -> None:
        try:
            payload = _CheckpointPayload(
                head=head,
                state=result.state,
                reports={revision: report.messages for revision, report in result.reports.items()},
                repository_descriptors=result.repository_descriptors,
                processed_cursors=result.processed_cursors,
            ).model_dump_json()
            self._save(prefix_digest, "checkpoint", payload)
        except (ValueError, TypeError):
            _LOGGER.warning("Could not encode graph history checkpoint", exc_info=True)

    def load_steps(self, prefix_digest: str) -> list[IndexedStep] | None:
        payload = self._load(prefix_digest, "steps")
        if payload is None:
            return None
        try:
            return _StepsPayload.model_validate_json(payload).steps
        except (ValueError, TypeError, KeyError):
            _LOGGER.warning("Invalid graph history step index; rebuilding", exc_info=True)
            return None

    def save_steps(self, prefix_digest: str, steps: list[IndexedStep]) -> None:
        try:
            self._save(prefix_digest, "steps", _StepsPayload(steps=steps).model_dump_json())
        except (ValueError, TypeError):
            _LOGGER.warning("Could not encode graph history step index", exc_info=True)

    def _path(self, prefix_digest: str, kind: str) -> Path:
        if _DIGEST.fullmatch(prefix_digest) is None:
            raise ValueError("invalid history prefix digest")
        # All three components belong to the cache owner, never a canonical tree.
        if any(
            path.is_symlink() for path in (self.root, self.root.parent, self.root.parent.parent)
        ):
            raise ValueError("unsafe graph history cache root")
        return self.root / f"{prefix_digest}.{kind}.json"

    def _load(self, prefix_digest: str, kind: str) -> str | None:
        try:
            path = self._path(prefix_digest, kind)
            with self.cache.pin([path]):
                if path.is_symlink() or path.stat().st_size > GRAPH_HISTORY_STORE_MAX_BYTES:
                    raise ValueError("unsafe or oversized graph history cache entry")
                envelope = _Envelope.model_validate_json(path.read_bytes())
                if (
                    envelope.prefix_digest != prefix_digest
                    or envelope.checksum != hashlib.sha256(envelope.payload.encode()).hexdigest()
                ):
                    raise ValueError("graph history cache identity or checksum mismatch")
                self.cache.touch(path)
                return envelope.payload
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            _LOGGER.warning("Could not read graph history cache; rebuilding", exc_info=True)
            return None

    def _save(self, prefix_digest: str, kind: str, payload: str) -> None:
        temporary: Path | None = None
        try:
            path = self._path(prefix_digest, kind)
            content = _Envelope(
                prefix_digest=prefix_digest,
                checksum=hashlib.sha256(payload.encode()).hexdigest(),
                payload=payload,
            ).model_dump_json()
            if len(content.encode()) > GRAPH_HISTORY_STORE_MAX_BYTES:
                _LOGGER.warning("Graph history cache entry exceeds the disk budget")
                return
            with self.cache.pin([path]):
                self.root.mkdir(parents=True, exist_ok=True)
                descriptor, name = tempfile.mkstemp(prefix=".history-", dir=self.root)
                temporary = Path(name)
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                directory = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                self.cache.touch(path)
            self.cache.sweep()
        except (OSError, ValueError):
            _LOGGER.warning("Could not write graph history cache", exc_info=True)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    _LOGGER.warning("Could not remove history cache temporary file", exc_info=True)
