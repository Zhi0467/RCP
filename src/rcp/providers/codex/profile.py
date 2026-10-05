"""Everything RCP knows about launching, containing, and decoding Codex."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from rcp.providers.base import (
    AgentCapability,
    ModelChoice,
    ProviderNativeUpdate,
    ProviderProfile,
    ProviderRuntime,
    ProviderRuntimeChoice,
    ProviderSkill,
    ProviderSkillProbe,
    ProviderStreamEvent,
    ProviderTurn,
    ProviderTurnRequest,
    ProviderUsage,
    _JsonlProviderRuntime,
    _optional_usage_int,
    _require_project_write_scope,
    _usage_dedupe_key,
    _usage_int,
)
from rcp.providers.browser_grant import BrowserGrant
from rcp.providers.codex.remote import AppServerTurnFence, CodexSessionFormat
from rcp.providers.turn_fence import TurnFence

if TYPE_CHECKING:
    from rcp.agents.write_scope import ProjectWriteScope
    from rcp.core.models import HiddenReadScope
    from rcp.provider_auth import ProviderAuthentication


class CodexProfile(ProviderProfile):
    @property
    def authentication(self) -> ProviderAuthentication:
        from rcp.providers.codex.auth import CodexAuthentication

        return CodexAuthentication()

    id = "codex"
    label = "Codex"
    usage_profile = "codex.turn.v1"
    local_session_roots_field = "codex_roots"
    remote_session_roots_field = "remote_codex_roots"
    # OpenAI's standalone installer is Codex's supported update path.
    native_update = ProviderNativeUpdate(
        installer_url="https://chatgpt.com/codex/install.sh",
        installer_env=("CODEX_NON_INTERACTIVE=1",),
    )
    session_format = CodexSessionFormat()
    legacy_runtime_id = "codex.exec-json.v1"
    default_runtime = "exec"
    runtime_aliases = {
        "exec": legacy_runtime_id,
        "exec-json": legacy_runtime_id,
        "app-server": "codex.app-server-stdio.v1",
        legacy_runtime_id: legacy_runtime_id,
        "codex.app-server-stdio.v1": "codex.app-server-stdio.v1",
    }
    turn_fences = {
        # `codex exec --json` ends its own turn with one terminal event.
        legacy_runtime_id: TurnFence,
        "codex.app-server-stdio.v1": AppServerTurnFence,
    }
    runtime_choices = (
        ProviderRuntimeChoice(id="exec", label="Codex exec"),
        ProviderRuntimeChoice(id="app-server", label="Codex app server"),
    )
    work_like_minimum_version = (0, 138, 0)

    def runtime(self, runtime_id: str) -> ProviderRuntime:
        if runtime_id == self.legacy_runtime_id:
            return _CodexExecRuntime(runtime_id, self)
        if runtime_id == "codex.app-server-stdio.v1":
            # The protocol adapter imports these shared runtime contracts, so
            # load it only after this module and the provider registry exist.
            from rcp.providers.codex.app_server import CodexAppServerRuntime

            return CodexAppServerRuntime()
        return super().runtime(runtime_id)

    def auth_command(self, binary: str) -> list[str]:
        return [binary, "login", "status"]

    def login_command(self, binary: str) -> list[str]:
        return [binary, "login"]

    def is_authenticated(self, result: subprocess.CompletedProcess[str]) -> bool:
        if result.returncode != 0:
            return False
        # "Not logged in" contains "logged in", so the negative has to be ruled
        # out first. Today a logged-out `codex login status` also exits non-zero,
        # which hid this; that is the CLI's choice to change, not ours to rely on.
        reported = (result.stdout + result.stderr).lower()
        return "not logged in" not in reported and "logged in" in reported

    def probe_failure_evidence(self, result: subprocess.CompletedProcess[str]) -> str:
        diagnostics = [result.stderr]
        for line in result.stdout.split("\n"):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                if result.returncode:
                    diagnostics.append(line)
                continue
            if isinstance(value, dict) and (
                "error" in value
                or value.get("method") == "error"
                or value.get("type") == "error"
                or self.decode_event(value, line).event == "error"
            ):
                diagnostics.append(line)
        return "\n".join(diagnostics)

    def credential_failure(self, stderr: str) -> bool:
        # Observed from a real run on 2026-09-12: `codex login status` still
        # reported a healthy login while every turn died on these. The run's
        # own diagnostic is the only trustworthy signal. On the same date a shared
        # login died between turns with refresh_token_reused / already-used text.
        reported = stderr.lower()
        return any(
            signature in reported
            for signature in (
                "refresh_token_reused",
                "refresh token was already used",
                "token_revoked",
                "refresh_token_invalidated",
                "refresh token was revoked",
                "your session has ended. please log in again",
            )
        )

    def login_probe_command(self, binary: str) -> list[str]:
        # Flags probed with codex-cli 0.154.0 on 2026-09-14.
        return [
            binary,
            "exec",
            "--ignore-user-config",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--model",
            "gpt-5.6-luna",
            "Reply with OK only. Do not use tools.",
        ]

    def catalog_command(self, binary: str) -> list[str] | None:
        return [binary, "debug", "models"]

    def parse_catalog(self, stdout: str) -> list[ModelChoice]:
        payload = json.loads(stdout)
        if not isinstance(payload, dict):
            return []
        entries = payload.get("models")
        if not isinstance(entries, list):
            return []
        choices: list[ModelChoice] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            # `hide` marks catalog rows Codex itself does not offer a human --
            # internal review models and the like.
            if entry.get("visibility") != "list":
                continue
            slug = entry.get("slug")
            if not isinstance(slug, str) or not slug:
                continue
            levels = [
                level["effort"]
                for level in entry.get("supported_reasoning_levels") or []
                if isinstance(level, dict) and isinstance(level.get("effort"), str)
            ]
            choices.append(
                ModelChoice(
                    id=slug,
                    label=entry.get("display_name") or slug,
                    reasoning=levels,
                    default_reasoning=entry.get("default_reasoning_level") or "",
                )
            )
        # Codex orders its own catalog by `priority`; preserve that rather than
        # imposing an alphabetical order the human has not seen anywhere else.
        return choices

    def skill_probe(self, binary: str) -> ProviderSkillProbe:
        return ProviderSkillProbe(
            command=[binary, "app-server"],
            protocol="jsonrpc",
            messages=(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {"name": "rcp", "version": "1"},
                        "capabilities": {},
                    },
                },
                {"jsonrpc": "2.0", "method": "initialized", "params": {}},
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "skills/list",
                    "params": {"cwds": ["/"], "forceReload": True},
                },
            ),
        )

    def parse_skills(self, payload: object) -> list[ProviderSkill]:
        if not isinstance(payload, dict):
            raise ValueError("Codex skills/list result is not an object")
        data = payload.get("data")
        if not isinstance(data, list):
            raise ValueError("Codex skills/list result has no data list")
        normalized: dict[str, ProviderSkill] = {}
        for scope in data:
            if not isinstance(scope, dict) or not isinstance(scope.get("skills"), list):
                continue
            for item in scope["skills"]:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                if not isinstance(name, str) or not name or item.get("enabled") is not True:
                    continue
                interface = item.get("interface")
                display_name = interface.get("displayName") if isinstance(interface, dict) else None
                description = item.get("description")
                normalized[name] = ProviderSkill(
                    name=name,
                    label=display_name if isinstance(display_name, str) and display_name else name,
                    description=description if isinstance(description, str) else "",
                    scope=item.get("scope") if isinstance(item.get("scope"), str) else None,
                    path=item.get("path") if isinstance(item.get("path"), str) else None,
                )
        return list(normalized.values())

    def native_skill_token(self, name: str) -> str:
        return f"${name}"

    def project_write_enforcement_mode(self) -> str:
        return "codex.permission-profile.v1"

    def command(
        self,
        prompt: str,
        *,
        binary: str,
        cwd: Path,
        model: str | None,
        reasoning: str | None,
        session_id: str | None,
        read_dirs: list[Path],
        write_dirs: list[Path],
        write_scope: ProjectWriteScope | None,
        capability: AgentCapability,
        provider_version: str | None,
        browser_grant: BrowserGrant | None = None,
        hidden_read_scope: HiddenReadScope | None = None,
    ) -> list[str]:
        del prompt, read_dirs
        command = [binary, "exec"]
        if session_id:
            # `codex exec resume` has no --sandbox or --cd; it takes the process
            # working directory and, left alone, codex's own default sandbox --
            # which is read-only. A resumed run must be able to write its patch
            # file, so the mode is set through --config.
            command.append("resume")
        command.extend(
            ["--json", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules"]
        )
        # Live retrieval is a provider tool, independent of whether command
        # execution is read-only or has workspace-write network access.
        command.extend(["--config", 'web_search="live"'])
        command.extend(_codex_environment_config(hidden_read_scope))
        if capability in {"work_auto", "orchestrate"}:
            scope = _require_project_write_scope(
                write_scope,
                capability=capability,
                write_dirs=write_dirs,
            )
            self.validate_readiness_version(provider_version, capability=capability)
            command.extend(
                [
                    "--config",
                    'default_permissions="rcp_project"',
                    "--config",
                    _codex_permission_profile(scope, hidden_read_scope),
                ]
            )
        else:
            if write_scope is not None:
                raise ValueError(f"capability {capability!r} cannot carry a project write scope")
            command.extend(["--config", 'approval_policy="never"'])
            if capability == "discuss":
                command.extend(
                    [
                        "--config",
                        'default_permissions="rcp_discuss"',
                        "--config",
                        _codex_discuss_permission_profile(cwd, hidden_read_scope),
                    ]
                )
            if capability != "discuss" and hidden_read_scope is not None:
                command.extend(
                    [
                        "--config",
                        'default_permissions="rcp_stage"',
                        "--config",
                        _codex_stage_permission_profile(
                            cwd, hidden_read_scope, read_only=capability == "paper_readonly"
                        ),
                    ]
                )
            sandbox = "read-only" if capability == "paper_readonly" else "workspace-write"
            if capability != "discuss" and hidden_read_scope is None:
                if session_id:
                    command.extend(["--config", f'sandbox_mode="{sandbox}"'])
                else:
                    command.extend(["--sandbox", sandbox])
            if capability not in {"paper_readonly", "discuss"} and hidden_read_scope is None:
                command.extend(["--config", "sandbox_workspace_write.network_access=true"])
        if not session_id:
            command.extend(["--cd", str(cwd)])
        if model:
            command.extend(["--model", model])
        if reasoning:
            command.extend(["--config", f'model_reasoning_effort="{reasoning}"'])
        if session_id:
            command.append(session_id)
        command.append("-")
        return command

    def decode_event(self, value: object, raw: str) -> ProviderStreamEvent:
        if not isinstance(value, dict):
            return ProviderStreamEvent(event="raw", text=raw)
        event_type = value.get("type", "")
        usage = self.decode_usage(value, raw)
        if event_type in {"thread.started", "session.started"}:
            return ProviderStreamEvent(
                event="session",
                session_id=value.get("thread_id") or value.get("session_id"),
                usage=usage,
            )
        if event_type in {"turn.failed", "error"}:
            error = value.get("error")
            if isinstance(error, dict):
                detail = error.get("message") or json.dumps(error, ensure_ascii=False)
            else:
                detail = error or value.get("message") or "Codex turn failed."
            return ProviderStreamEvent(
                event="error" if event_type == "turn.failed" else "message",
                text=str(detail),
                usage=usage,
            )
        item = value.get("item", {})
        if not isinstance(item, dict):
            item = {}
        text = item.get("text") or value.get("message") or ""
        if text:
            if item.get("type") == "agent_message" and event_type != "item.started":
                return ProviderStreamEvent(event="answer", text=str(text), usage=usage)
            return ProviderStreamEvent(event="message", text=str(text), usage=usage)
        return ProviderStreamEvent(event="raw", text=raw, usage=usage)

    def decode_usage(self, value: dict[str, object], raw: str) -> ProviderUsage | None:
        event_type = value.get("type")
        if event_type not in {"turn.completed", "turn.failed"}:
            return None
        usage = value.get("usage")
        if not isinstance(usage, dict):
            return None
        input_tokens = _usage_int(usage.get("input_tokens"))
        output_tokens = _usage_int(usage.get("output_tokens"))
        cached_input_tokens = _usage_int(usage.get("cached_input_tokens"))
        cache_write_input_tokens = _usage_int(usage.get("cache_write_input_tokens"))
        reasoning_output_tokens = _usage_int(usage.get("reasoning_output_tokens"))
        return ProviderUsage(
            provider_profile=self.usage_profile,
            provider_event_type=str(event_type),
            dedupe_key=_usage_dedupe_key(value, raw, "turn_id", "id", "event_id"),
            processed_input_tokens=input_tokens,
            generated_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            cache_write_input_tokens=cache_write_input_tokens,
            reasoning_output_tokens=reasoning_output_tokens,
            reported_input_tokens=input_tokens,
            reported_output_tokens=output_tokens,
            reported_total_tokens=_optional_usage_int(usage.get("total_tokens")),
            provider_fields={str(key): item for key, item in usage.items()},
        )


def _codex_permission_profile(
    scope: ProjectWriteScope, hidden_read_scope: HiddenReadScope | None = None
) -> str:
    roots = ",".join(
        f"{json.dumps(path, ensure_ascii=False)}=true" for path in scope.writable_roots
    )
    protected = ",".join(
        f'{json.dumps(path, ensure_ascii=False)}="read"' for path in scope.protected_write_paths
    )
    denials = _codex_read_denials(hidden_read_scope)
    # Codex protects `.git` separately, so the writable root alone does not let
    # Work run ordinary Git commands such as fetch or pull. `.research` is "read",
    # never "deny": Codex reads `deny` as no access at all, which would also revoke
    # the canonical run context. Invariant 4 protects that state from *writes*.
    return (
        "permissions={rcp_project={workspace_roots={"
        + roots
        + '},filesystem={":root"="read",":workspace_roots"={"."="write",'
        '".git"="write",".research"="read"}'
        + ("," + protected if protected else "")
        + ("," + denials if denials else "")
        + "},network={enabled=true}}}"
    )


def _codex_read_denials(hidden_read_scope: HiddenReadScope | None = None) -> str:
    if hidden_read_scope is None:
        from rcp.agents.hidden_read import WEBKIT_READ_DENY_PATHS

        return ",".join(f'{json.dumps(path)}="deny"' for path in WEBKIT_READ_DENY_PATHS)
    paths = (
        *hidden_read_scope.hidden_directories,
        *hidden_read_scope.hidden_files,
        *hidden_read_scope.hidden_globs,
    )
    return ",".join(f'{json.dumps(path)}="deny"' for path in paths)


def _codex_shell_environment_policy(scope: HiddenReadScope | None) -> dict[str, object]:
    from rcp.agents.hidden_read import HIDDEN_READ_ENV_ALLOW_LIST

    allowed = list(scope.env_allow_list if scope else HIDDEN_READ_ENV_ALLOW_LIST)
    return {
        # An empty allow list is the explicit unhidden fallback: preserve the
        # original tool environment, including Git and SSH authentication.
        "inherit": "all",
        "include_only": allowed,
        "ignore_default_excludes": True,
        "exclude": [],
        "set": {},
    }


def _codex_environment_config(scope: HiddenReadScope | None) -> list[str]:
    policy = _codex_shell_environment_policy(scope)
    return [
        "--config",
        "shell_environment_policy={inherit="
        + json.dumps(policy["inherit"])
        + ",ignore_default_excludes=true,"
        "exclude=[],set={},include_only=" + json.dumps(policy["include_only"]) + "}",
    ]


def _codex_stage_permission_profile(
    cwd: Path, hidden_read_scope: HiddenReadScope, *, read_only: bool
) -> str:
    filesystem = '":root"="read"'
    if not read_only:
        filesystem += ',":workspace_roots"={"."="write"},":tmpdir"="write",":slash_tmp"="write"'
    denials = _codex_read_denials(hidden_read_scope)
    if denials:
        filesystem += "," + denials
    return (
        "permissions={rcp_stage={workspace_roots={"
        + json.dumps(str(cwd))
        + "=true},filesystem={"
        + filesystem
        + "},network={enabled=true}}}"
    )


def _codex_discuss_permission_profile(
    cwd: Path, hidden_read_scope: HiddenReadScope | None = None
) -> str:
    denials = _codex_read_denials(hidden_read_scope)
    return (
        "permissions={rcp_discuss={workspace_roots={"
        + json.dumps(str(cwd))
        + '=true},filesystem={":root"="read",'
        '":workspace_roots"={"."="write"},":tmpdir"="write",":slash_tmp"="write"'
        + ("," + denials if denials else "")
        + "},network={enabled=true}}}"
    )


class _CodexExecRuntime(_JsonlProviderRuntime):
    def turn(self, request: ProviderTurnRequest) -> ProviderTurn:
        if request.legacy_command is None:
            command = self._profile.command(
                request.prompt,
                binary=request.binary,
                cwd=request.cwd,
                model=request.model,
                reasoning=request.reasoning,
                session_id=request.session_id,
                read_dirs=request.read_dirs,
                write_dirs=request.write_dirs,
                write_scope=request.write_scope,
                capability=request.capability,
                provider_version=request.provider_version,
                browser_grant=request.browser_grant,
                hidden_read_scope=request.hidden_read_scope,
            )
            request = replace(request, legacy_command=command)
        return super().turn(request)


def __getattr__(name: str) -> object:
    # Compatibility for callers of the previous provider-owned defaults. A
    # deferred alias avoids cycling through the policy resolver's registry import.
    if name == "CODEX_READ_DENY_PATHS":
        from rcp.agents.hidden_read import WEBKIT_READ_DENY_PATHS

        return WEBKIT_READ_DENY_PATHS
    raise AttributeError(name)
