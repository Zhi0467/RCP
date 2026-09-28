"""How a member signs Codex in, verifies it, and signs it out."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

from rcp.provider_auth import DeviceLogin, DeviceLoginStep, ProviderAuthentication

if TYPE_CHECKING:
    from rcp.agents.provider_environment import ProviderCredentialStore


class CodexDeviceLogin(DeviceLogin):
    """Codex device sign-in over the app-server JSON protocol.

    Every fact RCP acts on is a protocol field: `userCode` and `verificationUrl`
    from the `account/login/start` reply, then `success` on the
    `account/login/completed` notification. Codex may reword its console output
    or its error strings without changing what RCP reads.
    """

    INITIALIZE_ID = 1
    START_ID = 2
    CANCEL_ID = 3

    def __init__(self) -> None:
        self._login_id: str | None = None

    def command(self, binary: str) -> list[str]:
        return [binary, "app-server"]

    def initial_input(self) -> bytes:
        return _rpc_bytes(
            {
                "id": self.INITIALIZE_ID,
                "method": "initialize",
                "params": {"clientInfo": {"name": "rcp", "title": "RCP", "version": "1"}},
            }
        )

    def receive_line(self, line: str) -> DeviceLoginStep:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            return DeviceLoginStep()
        if not isinstance(value, dict):
            return DeviceLoginStep()
        if value.get("id") == self.INITIALIZE_ID and "error" in value:
            # The provider answered but refused to start. It may hold the
            # connection open, so end here instead of waiting for a reply that
            # will never come.
            return DeviceLoginStep(
                finished=True,
                failure=_protocol_error_text(value.get("error"))
                or "The provider refused to start a sign-in session.",
            )
        if value.get("id") == self.INITIALIZE_ID and "result" in value:
            return DeviceLoginStep(
                send=_rpc_bytes({"method": "initialized", "params": {}})
                + _rpc_bytes(
                    {
                        "id": self.START_ID,
                        "method": "account/login/start",
                        "params": {"type": "chatgptDeviceCode"},
                    }
                )
            )
        if value.get("id") == self.START_ID:
            return self._started(value)
        if value.get("method") == "account/login/completed":
            return self._completed(value.get("params"))
        if "id" in value and "method" in value:
            # A request from the provider has no unattended answer; end the attempt.
            return DeviceLoginStep(
                finished=True, failure="The provider asked for input RCP cannot answer."
            )
        return DeviceLoginStep()

    def _started(self, value: dict[str, object]) -> DeviceLoginStep:
        if "error" in value:
            return DeviceLoginStep(finished=True, failure=_protocol_error_text(value.get("error")))
        result = value.get("result")
        if not isinstance(result, dict):
            return DeviceLoginStep(finished=True, failure="The provider started no device login.")
        self._login_id = str(result.get("loginId") or "") or None
        fields = {}
        code = result.get("userCode")
        url = result.get("verificationUrl")
        if isinstance(code, str) and code:
            fields["user_code"] = code
        if isinstance(url, str) and url:
            fields["verification_url"] = url
        if len(fields) < 2:
            return DeviceLoginStep(
                finished=True, failure="The provider returned no device code to enter."
            )
        return DeviceLoginStep(fields=fields)

    def _completed(self, params: object) -> DeviceLoginStep:
        if not isinstance(params, dict):
            return DeviceLoginStep()
        if self._login_id and params.get("loginId") not in (None, self._login_id):
            return DeviceLoginStep()
        if params.get("success") is True:
            return DeviceLoginStep(finished=True)
        # A refusal the provider did not explain is still a refusal: without a
        # failure here the caller would go on to verify, and a still-valid old
        # credential would report the replacement login as signed in.
        return DeviceLoginStep(
            finished=True,
            failure=_protocol_error_text(params.get("error"))
            or "The provider reported the sign-in as unsuccessful.",
        )

    def cancel_input(self) -> bytes:
        if not self._login_id:
            return b""
        return _rpc_bytes(
            {
                "id": self.CANCEL_ID,
                "method": "account/login/cancel",
                "params": {"loginId": self._login_id},
            }
        )


def _protocol_error_text(value: object) -> str:
    """The provider's own words for a failure; displayed, never matched on."""

    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        message = value.get("message")
        if isinstance(message, str):
            return message
    return ""


def _rpc_bytes(value: dict[str, object]) -> bytes:
    return (json.dumps(value, separators=(",", ":")) + "\n").encode("utf-8")


class CodexAuthentication(ProviderAuthentication):
    supports_sign_out = True
    methods = ("device_code",)

    def verification_succeeded(self, result: subprocess.CompletedProcess[str]) -> bool:
        from rcp.providers.codex.profile import CodexProfile

        output = subprocess.CompletedProcess(result.args, result.returncode, result.stdout, "")
        return (
            super().verification_succeeded(result)
            and not CodexProfile().probe_failure_evidence(output).strip()
        )

    def device_login(self) -> DeviceLogin:
        return CodexDeviceLogin()

    def sign_out(self, credentials: ProviderCredentialStore, host: str, binary: str) -> list[str]:
        return [binary, "logout"]
