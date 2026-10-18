from __future__ import annotations

import re
from typing import get_args

from pydantic import TypeAdapter

from rcp.core.models import ProjectNode, graph_fields

NODE_PREFIXES = {
    "research_question": "rq",
    "hypothesis": "hyp",
    "decision": "dec",
    "experiment": "exp",
    "evidence": "ev",
    "blocker": "blk",
}
SLUG_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*/[a-z0-9]+(?:-[a-z0-9]+)*$")
IDENTIFIER_RE = re.compile(r"\b(?:[a-z]+_[a-z0-9_]+|[a-z]+-[a-z0-9-]*\d[a-z0-9-]*)\b")
NODE_ADAPTER = TypeAdapter(ProjectNode)
_NODE_MODELS = get_args(get_args(ProjectNode)[0])
IMMUTABLE_NODE_UPDATE_FIELDS = frozenset().union(
    *(
        graph_fields(model, node_update=policy)
        for model in _NODE_MODELS
        for policy in ("immutable", "replay_only")
    )
)

# Loading a pre-generation-2 Patch migrates retired vocabulary in memory, which
# adds these system fields to operations that never carried them on disk. Replay
# must accept them where a live write never could -- see
# `adapt_persisted_patch_document`.
LEGACY_COMPATIBILITY_UPDATE_FIELDS = frozenset().union(
    *(graph_fields(model, node_update="replay_only") for model in _NODE_MODELS)
)
