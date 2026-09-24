"""Resolve and backfill repository credentials on the provider execution host."""

from __future__ import annotations

import asyncio
import importlib.resources
import shlex

from rcp.git_access import ensure_checkout_git_access, missing_deploy_key
from rcp.limits import SERVER_PROJECT_CHECKOUT_TIMEOUT_SECONDS
from rcp.transport.ssh import ssh_arguments


async def ensure_team_checkout_access(
    checkouts: list[tuple[str, str]], *, host: str = ""
) -> list[str]:
    """Backfill every checkout; return one notice per repository without a key."""
    notices: list[str] = []
    for repository, key in checkouts:
        if not host:
            resolved_key = await asyncio.to_thread(
                ensure_checkout_git_access,
                repository,
                key,
                timeout=SERVER_PROJECT_CHECKOUT_TIMEOUT_SECONDS,
            )
            if resolved_key is None:
                notices.append(missing_deploy_key(repository))
            continue
        source = importlib.resources.files("rcp").joinpath("git_access.py").read_text()
        command = shlex.join(
            ("python3", "-c", source, repository, key, str(SERVER_PROJECT_CHECKOUT_TIMEOUT_SECONDS))
        )
        process = await asyncio.create_subprocess_exec(
            *ssh_arguments(host, command),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            output, error = await asyncio.wait_for(
                process.communicate(),
                timeout=SERVER_PROJECT_CHECKOUT_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise RuntimeError("Timed out configuring the remote repository deploy key.") from None
        if process.returncode:
            raise RuntimeError(error.decode(errors="replace").strip())
        if not output.decode().strip():
            notices.append(missing_deploy_key(repository))
    return notices
