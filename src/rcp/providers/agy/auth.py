"""Antigravity machine sign-in verification and browser-free process setup."""

from __future__ import annotations

import importlib.resources
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from rcp.provider_auth import DeviceLogin, DeviceLoginStep, ProviderAuthentication

if TYPE_CHECKING:
    from rcp.agents.provider_environment import ProviderCredentialStore, ProviderProcessEnvironment
    from rcp.storage.models import ProviderLoginStateRecord


TOKEN_PATH = ".gemini/antigravity-cli/antigravity-oauth-token"


def process_prefix(
    *, host: str, data_dir: Path | None = None, home: str | None = None
) -> list[str]:
    """Use the installed RCP runtime locally and ship its helper to remote hosts."""
    if not host:
        command = [
            sys.executable,
            *(() if getattr(sys, "frozen", False) else ("-m", "rcp")),
            "_agy-run",
        ]
        if data_dir is not None:
            command.extend(["--data-dir", str(data_dir)])
        if home is not None:
            command.extend(["--home", home])
        return [*command, "--"]
    sources = (
        (
            "rcp.providers.login_homes",
            importlib.resources.files("rcp.providers").joinpath("login_homes.py").read_text(),
        ),
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
    options = ["--home", home] if home is not None else []
    return ["python3", "-c", program, "--agy-run", *options, "--"]


class AgyPasteLogin(DeviceLogin):
    accepts_code = True

    def command(self, binary: str) -> list[str]:
        return [binary, "acp", "--login"]

    def receive_line(self, line: str) -> DeviceLoginStep:
        match = re.search(r"https://accounts\.google\.com/[^\s<>\"']+", line)
        if match:
            return DeviceLoginStep(fields={"verification_url": match.group(0)})
        return DeviceLoginStep()

    def code_input(self, code: str) -> bytes:
        return (code + "\n").encode("utf-8")


class AgyAuthentication(ProviderAuthentication):
    methods = ("paste_code",)
    supports_sign_out = True

    def device_login(self) -> DeviceLogin:
        return AgyPasteLogin()

    def credential_available(
        self,
        credentials: ProviderCredentialStore,
        machine_id: str,
        login_id: str,
        *,
        login_state: ProviderLoginStateRecord | None = None,
    ) -> bool:
        credentials.account_dir("agy", machine_id, login_id)
        return not login_id or (
            login_state is not None
            and login_state.state == "signed_in"
            and login_state.removed_at is None
        )

    def unmanaged_environment(self, host: str) -> ProviderProcessEnvironment:
        from rcp.agents.provider_environment import ProviderProcessEnvironment

        return ProviderProcessEnvironment(command_prefix=tuple(process_prefix(host=host)))

    def process_environment(
        self, credentials: ProviderCredentialStore, machine_id: str, login_id: str, *, host: str
    ) -> ProviderProcessEnvironment:
        from rcp.agents.provider_environment import ProviderProcessEnvironment

        credentials.account_dir("agy", machine_id, login_id)
        home = None
        if login_id:
            home = (
                f"~/.rcp/provider-homes/agy/{login_id}"
                if host
                else str(credentials.login_home("agy", login_id))
            )
        return ProviderProcessEnvironment(
            command_prefix=tuple(
                process_prefix(host=host, data_dir=credentials.root.parent, home=home)
            )
        )

    def sign_out(
        self,
        credentials: ProviderCredentialStore,
        machine_id: str,
        login_id: str,
        binary: str,
        *,
        host: str,
    ) -> list[str] | None:
        credentials.account_dir("agy", machine_id, login_id)
        if not host:
            home = credentials.login_home("agy", login_id) if login_id else Path.home()
            (home / TOKEN_PATH).unlink(missing_ok=True)
            return None
        from rcp.limits import PROVIDER_TOKEN_PLACEMENT_TIMEOUT_SECONDS
        from rcp.transport.ssh import ssh_arguments

        source = (
            importlib.resources.files("rcp.server_ops").joinpath("provider_cleanup.py").read_text()
        )
        relative = f".rcp/provider-homes/agy/{{login_id}}/{TOKEN_PATH}" if login_id else TOKEN_PATH
        command = shlex.join(["python3", "-c", source, login_id, relative])
        result = subprocess.run(
            ssh_arguments(host, command),
            capture_output=True,
            timeout=PROVIDER_TOKEN_PLACEMENT_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode:
            raise ValueError("Could not remove Antigravity credential.")
        return None

    def verification_succeeded(self, result: subprocess.CompletedProcess[str]) -> bool:
        if result.returncode:
            return False
        try:
            value = json.loads(result.stdout)
        except (ValueError, TypeError):
            return False
        return isinstance(value, dict) and value.get("status") == "SUCCESS"
