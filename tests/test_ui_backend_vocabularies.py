"""Staging guard for backend vocabularies the web layer can still name.

The end state is not detection. A vocabulary is finished when its exported type
is opaque, as `EpisodeStatus` is in `web/src/types.ts`: the client receives the
lifecycle already decided, and a comparison against a status literal does not
compile. That makes the derivation impossible to write rather than possible to
find afterwards.

This guard covers the vocabularies that have not reached that state yet, so they
cannot spread while they wait. A vocabulary graduates out of this file when it is
sealed by branding, which is why `_LIVE_EPISODE_STATUSES` is no longer here.
"""

from __future__ import annotations

import re
from pathlib import Path

from rcp.storage.models import ACTIVE_AGENT_TASK_STATUSES

WEB_SOURCE = Path(__file__).resolve().parents[1] / "web" / "src"

GUARDED_VOCABULARIES = {
    "ACTIVE_AGENT_TASK_STATUSES": frozenset(ACTIVE_AGENT_TASK_STATUSES),
}

# `types.ts` is the one sanctioned restatement of a backend response shape, so its
# status unions necessarily name every member.
EXEMPT_FILES = {"types.ts"}

# The copies that exist today, kept visible rather than silently tolerated. They
# retire together when task lifecycle is exported decided and `AgentTaskStatus` is
# sealed the way `EpisodeStatus` now is. This list is closed: it may shrink, never
# grow.
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
