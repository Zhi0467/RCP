"""Fix commands RCP shows a human, written from where that human types them.

A shown command is the whole line for the person's own prompt. It never assumes
they are the account RCP runs as:

- A personal space runs RCP as the human. A local command is shown as is; a
  remote one goes through ``ssh -t <host>``.
- A team server runs RCP as its service account, while the human types in their
  own login there, with sudo. Root work takes ``sudo``, work as the service
  account takes ``sudo -u <svc> -H``, and a remote machine is reached through the
  service account's SSH, because its route lives in that account's SSH config.

Work that needs root on the far machine carries its own ``sudo`` inside the SSH
command. Every part is quoted with `shlex`, so the line pastes as one command.
"""

from __future__ import annotations

import os
import pwd
import shlex
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class OperatorRoute:
    """How a human reaches one machine's shell; ``host`` "" is RCP's own machine."""

    host: str = ""
    #: The account RCP runs as when that is not the human (a team server); "" otherwise.
    service_account: str = ""

    def command(self, argv: Sequence[str], *, root: bool = False) -> str:
        """`argv` as the human types it; ``root`` runs it as root on that machine."""
        on_machine = ["sudo", *argv] if root else list(argv)
        if not self.host and root:
            return shlex.join(on_machine)
        line = ["ssh", "-t", self.host, shlex.join(on_machine)] if self.host else on_machine
        account = ["sudo", "-u", self.service_account, "-H"] if self.service_account else []
        return shlex.join([*account, *line])


def operator_route(space_kind: str, host: str = "") -> OperatorRoute:
    """The route to `host` for this space; a team server's RCP runs as its service account."""
    account = pwd.getpwuid(os.getuid()).pw_name if space_kind == "team" else ""
    return OperatorRoute(host=host, service_account=account)
