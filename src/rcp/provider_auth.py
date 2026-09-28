"""Provider-owned authentication mechanics; no task or episode authority."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rcp.agents.provider_environment import ProviderProcessEnvironment

if TYPE_CHECKING:
    from rcp.agents.provider_environment import ProviderCredentialStore


@dataclass(frozen=True)
class DeviceLoginStep:
    """What one line of a provider's device-login protocol means to RCP.

    `fields` carry the code and link a human needs, read from the provider's own
    structured reply rather than from prose it prints. `send` is the next frame
    to write. A step is terminal when `finished` is set; `failure` then holds the
    provider's own explanation, which RCP shows but never interprets.
    """

    fields: dict[str, str] = field(default_factory=dict)
    send: bytes | None = None
    finished: bool = False
    failure: str | None = None


class DeviceLogin:
    """One provider's device sign-in, driven over that provider's structured protocol."""

    def command(self, binary: str) -> list[str]:
        raise NotImplementedError

    def initial_input(self) -> bytes:
        return b""

    def receive_line(self, line: str) -> DeviceLoginStep:
        raise NotImplementedError

    def cancel_input(self) -> bytes:
        return b""


class ProviderAuthentication:
    #: Variables every process of this provider starts with, managed or not.
    process_variables: dict[str, str] = {}
    supports_sign_out = False
    methods: tuple[str, ...] = ()
    token_instructions: str | None = None
    missing_credential_detail = "No managed credential is saved."

    def credential_available(self, credentials: ProviderCredentialStore, host: str) -> bool:
        return True

    def credential_metadata(self, credentials: ProviderCredentialStore, host: str) -> dict | None:
        return None

    def process_environment(
        self, credentials: ProviderCredentialStore, host: str
    ) -> ProviderProcessEnvironment:
        return ProviderProcessEnvironment()

    def unmanaged_environment(self, host: str) -> ProviderProcessEnvironment:
        """The environment when RCP manages no credential and the CLI's own login applies."""
        return ProviderProcessEnvironment().with_variables(
            self.process_variables, remote=bool(host)
        )

    def device_login(self) -> DeviceLogin:
        raise ValueError("Device sign-in is not supported by this provider.")

    def validate_token(self, token: str) -> str:
        raise ValueError("Token entry is not supported by this provider.")

    def save_token(
        self,
        credentials: ProviderCredentialStore,
        host: str,
        token: str,
        *,
        member_id: str,
        now: str,
    ) -> None:
        raise ValueError("Token entry is not supported by this provider.")

    def prepare_verification(self, credentials: ProviderCredentialStore, host: str) -> None:
        pass

    def verified(self, credentials: ProviderCredentialStore, host: str, *, now: str) -> None:
        pass

    def sign_out(
        self, credentials: ProviderCredentialStore, host: str, binary: str
    ) -> list[str] | None:
        raise ValueError("Sign-out is not supported by this provider.")

    def verification_succeeded(self, result: subprocess.CompletedProcess[str]) -> bool:
        return result.returncode == 0 and bool(result.stdout.strip())
