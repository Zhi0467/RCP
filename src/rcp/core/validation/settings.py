from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from rcp.config import Manifest
    from rcp.core.models import GraphState


@dataclass(frozen=True)
class ValidationContext:
    project_truth_scope: Iterable[str]
    repository_aliases: Iterable[str] | None = None
    machine_aliases: Iterable[str] | None = None
    state_repository: str | None = None
    mode: Literal["admission", "replay"] = "admission"
    # A branch merge's source head; content it carries verbatim skips content rules.
    merge_source: GraphState | None = None


def build_validation_context(
    manifest: Manifest,
    state: GraphState,
    *,
    mode: Literal["admission", "replay"] = "admission",
) -> ValidationContext:
    return ValidationContext(
        project_truth_scope=tuple(state.project_truth_scope),
        repository_aliases=tuple(manifest.repository_map),
        machine_aliases=tuple(manifest.machine_map),
        state_repository=manifest.state.repository,
        mode=mode,
    )
