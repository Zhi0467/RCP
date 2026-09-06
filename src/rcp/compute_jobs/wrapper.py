"""Backend-independent wrapper and immutable launch provenance."""

from __future__ import annotations

import json
import shlex
from pathlib import PurePosixPath

from rcp.compute_jobs.models import ComputeLaunchRequest


def render_wrapper(job_root: str, request: ComputeLaunchRequest) -> str:
    root = PurePosixPath(job_root)
    quote = shlex.quote
    return f"""#!/bin/sh
umask 077
cd {quote(str(root))} || exit 1
date +%s > {quote(str(root / "started"))}
if [ -r /proc/self/cgroup ]; then
    cat /proc/self/cgroup > {quote(str(root / "cgroup"))}
fi
(
    cd {quote(request.cwd)} && {shlex.join(request.argv)}
) >> {quote(str(root / "log"))} 2>&1
status=$?
printf '%s %s\\n' "$status" "$(date +%s)" > {quote(str(root / ".exit.tmp"))}
mv {quote(str(root / ".exit.tmp"))} {quote(str(root / "exit"))}
exit "$status"
"""


def command_provenance(
    request: ComputeLaunchRequest,
    *,
    project_id: str,
    origin_operation_id: str,
    episode_id: str | None,
) -> str:
    return (
        json.dumps(
            {
                **request.model_dump(mode="json"),
                "project_id": project_id,
                "origin_operation_id": origin_operation_id,
                "episode_id": episode_id,
            },
            sort_keys=True,
        )
        + "\n"
    )
