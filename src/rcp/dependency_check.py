"""Check a machine against `rcp.dependencies`.

The local machine is read in this process: `platform.system()`, `/etc/os-release`,
and `shutil.which` with the backend's own PATH. Every other check runs the fixed
shipped script `staged_dependency_check.sh` through a caller's command prefix
(the plain SSH route for a remote machine, `runuser` for the server's service
account), so it sees the account and PATH that real work sees.

Only a definite answer refuses a run. Unreachable, timed out, and unreadable
answers are ``not_checked`` and never refuse.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Collection

from rcp.core.models import DependencyStatus
from rcp.dependencies import Role

# Runs one argv with the script on stdin and a timeout; the subprocess seam tests replace.
Runner = Callable[[list[str], str, float], subprocess.CompletedProcess[str]]
# Wraps the script's own argv (`sh -s -- <nonce> <names...>`) for one execution route.
Route = Callable[[list[str]], list[str]]


def staged_check_script() -> str:
    """The shipped POSIX script, read from the package."""
    raise NotImplementedError


def script_check(
    route: Route,
    roles: Collection[Role],
    *,
    runner: Runner | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> DependencyStatus:
    """Run the shipped script through `route` for these Linux roles, with bounded retry."""
    raise NotImplementedError


def local_check(roles: Collection[Role]) -> DependencyStatus:
    """Check this process's own machine for these roles, without a shell."""
    raise NotImplementedError


class DependencyChecker:
    """One per backend process; admission and the machine card share it.

    Host ``""`` is the local machine (role ``local``); any other host is a remote
    Linux machine (role ``remote``) reached over the plain SSH route the state
    transport uses. Results are cached per host. One lock per host means
    concurrent callers for one machine share a single check and different
    machines never wait on each other.
    """

    def __init__(
        self,
        *,
        runner: Runner | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        local: Callable[[Collection[Role]], DependencyStatus] = local_check,
    ) -> None:
        raise NotImplementedError

    def status(self, host: str, *, refresh: bool = False) -> DependencyStatus:
        """The cached result when fresh, otherwise a new check."""
        raise NotImplementedError

    def launch_refusal(self, host: str) -> str | None:
        """A reason to refuse an agent run on `host`, or None to admit it.

        A cached ``missing`` or ``unsupported`` is rechecked before it refuses.
        ``not_checked`` admits.
        """
        raise NotImplementedError
