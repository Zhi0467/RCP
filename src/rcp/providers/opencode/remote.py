"""OpenCode's turn boundaries, as an execution host reads them.

Shipped as source text to execution hosts by `rcp.providers.remote_bundle`,
after the shared bases, so it may import only the standard library. The guarded
import serves the local copy. OpenCode keeps its sessions in one SQLite
database rather than in files, so RCP does not index them yet and there is no
session format here.
"""

from __future__ import annotations

if "TurnFence" not in globals():
    from rcp.providers.turn_fence import TurnFence


#: The line RCP's launch wrapper prints once `opencode run` has exited.
EXIT_MARKER = "rcp.provider_exit"


class OpenCodeRunTurnFence(TurnFence):
    """`opencode run --format json`, ended by RCP's report of its exit."""

    def _output(self, value: dict) -> None:
        # OpenCode itself prints nothing at the end of a turn, and an error is
        # followed by its exit anyway. The launch wrapper adds this line once the
        # process has exited.
        if value.get("type") == EXIT_MARKER:
            self.terminal = True
