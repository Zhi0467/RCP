"""The web layer must not restate a status vocabulary the backend decides with.

Copying a backend status set into a component is how one Experiment came to be
startable in the node panel and unstartable in Runs: both were answering the
same question, one from a published field and one from a hand-written list. The
copies were identical, so no equality check would have found them. What makes a
copy wrong is that it exists at all, because it re-implements a rule whose owner
is the server.

The remedy for a flagged file is to publish the fact the UI is reconstructing,
the way `EpisodeResponse.live` and `ExperimentControlState.graph_reasons` are
published, and read the field.
"""

from __future__ import annotations

import re
from pathlib import Path

from rcp.storage.episodes import _LIVE_EPISODE_STATUSES
from rcp.storage.models import ACTIVE_AGENT_TASK_STATUSES

WEB_SOURCE = Path(__file__).resolve().parents[1] / "web" / "src"

GUARDED_VOCABULARIES = {
    "_LIVE_EPISODE_STATUSES": frozenset(_LIVE_EPISODE_STATUSES),
    "ACTIVE_AGENT_TASK_STATUSES": frozenset(ACTIVE_AGENT_TASK_STATUSES),
}

# `types.ts` is the one sanctioned restatement of a backend response shape, so its
# status unions necessarily name every member.
EXEMPT_FILES = {"types.ts"}

# Known duplications of the active-task vocabulary, kept visible rather than
# silently tolerated. Each wants a published per-task field. This list is closed:
# it may shrink, never grow.
KNOWN_DUPLICATIONS = {
    ("runProjection.ts", "ACTIVE_AGENT_TASK_STATUSES"),
    ("components/ExperimentRunDetail.tsx", "ACTIVE_AGENT_TASK_STATUSES"),
    ("agentTasks.ts", "ACTIVE_AGENT_TASK_STATUSES"),
}

_BRACKETED = re.compile(r"\[[^\[\]]*\]", re.DOTALL)
_QUOTED = re.compile(r'"([a-z_]+)"')


def _spans(source: str) -> list[str]:
    """One literal collection, or one line, is a small enough place to restate a set."""

    return [*_BRACKETED.findall(source), *source.splitlines()]


def _duplications() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted(WEB_SOURCE.rglob("*.ts*")):
        relative = path.relative_to(WEB_SOURCE).as_posix()
        if relative in EXEMPT_FILES:
            continue
        source = path.read_text(encoding="utf-8")
        for span in _spans(source):
            literals = set(_QUOTED.findall(span))
            for name, vocabulary in GUARDED_VOCABULARIES.items():
                if vocabulary <= literals:
                    found.add((relative, name))
    return found


def test_the_web_layer_does_not_restate_a_backend_status_vocabulary() -> None:
    unexpected = _duplications() - KNOWN_DUPLICATIONS
    assert not unexpected, (
        "These web modules restate a status set the backend decides with: "
        + ", ".join(f"{path} copies {name}" for path, name in sorted(unexpected))
        + ". Publish the fact the module is reconstructing as a field on the "
        "payload and read it, rather than answering the backend's question here."
    )


def test_every_known_duplication_still_exists() -> None:
    """A retired duplication leaves this list, so the debt cannot quietly regrow."""

    assert _duplications() >= KNOWN_DUPLICATIONS
