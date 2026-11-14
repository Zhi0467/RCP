"""Antigravity machine sign-in verification and browser-free process setup."""

from __future__ import annotations

import importlib.resources
import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from rcp.provider_auth import ProviderAuthentication

if TYPE_CHECKING:
    from rcp.agents.provider_environment import ProviderCredentialStore, ProviderProcessEnvironment


def process_prefix(*, host: str, data_dir: Path | None = None) -> list[str]:
    """Use the installed RCP runtime locally and ship its helper to remote hosts."""
    if not host:
        command = [
            sys.executable,
            *(() if getattr(sys, "frozen", False) else ("-m", "rcp")),
            "_agy-run",
        ]
        if data_dir is not None:
            command.extend(["--data-dir", str(data_dir)])
        return [*command, "--"]
    sources = (
        (
            "rcp.providers.turn_fence",
            importlib.resources.files("rcp.providers").joinpath("turn_fence.py").read_text(),
        ),
        (
            "rcp.providers.agy.remote",
            importlib.resources.files("rcp.providers.agy").joinpath("remote.py").read_text(),
        ),
    )
    program = "\n".join(
        f"exec(compile({source!r}, {module!r}, 'exec'))" for module, source in sources
    )
    return ["python3", "-c", program, "--agy-run", "--"]


class AgyAuthentication(ProviderAuthentication):
    methods = ()

    def unmanaged_environment(self, host: str) -> ProviderProcessEnvironment:
        from rcp.agents.provider_environment import ProviderProcessEnvironment

        return ProviderProcessEnvironment(command_prefix=tuple(process_prefix(host=host)))

    def process_environment(
        self, credentials: ProviderCredentialStore, machine_id: str, login_id: str, *, host: str
    ) -> ProviderProcessEnvironment:
        from rcp.agents.provider_environment import ProviderProcessEnvironment

        return ProviderProcessEnvironment(
            command_prefix=tuple(process_prefix(host=host, data_dir=credentials.root.parent))
        )

    def verification_succeeded(self, result: subprocess.CompletedProcess[str]) -> bool:
        if result.returncode:
            return False
        try:
            value = json.loads(result.stdout)
        except (ValueError, TypeError):
            return False
        return isinstance(value, dict) and value.get("status") == "SUCCESS"
