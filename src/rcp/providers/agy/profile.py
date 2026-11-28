"""Antigravity launch policy and its queued stream-JSON protocol."""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from rcp.providers.agy.remote import AgyStreamTurnFence, agy_agent_name
from rcp.providers.base import (
    AgentCapability,
    AgentCompaction,
    AgentStatusDelta,
    ModelChoice,
    ProviderLimitProbe,
    ProviderNativeUpdate,
    ProviderProfile,
    ProviderRuntime,
    ProviderRuntimeChoice,
    ProviderRuntimeStep,
    ProviderSkill,
    ProviderSkillProbe,
    ProviderSteeringState,
    ProviderStreamEvent,
    ProviderTurn,
    ProviderTurnRequest,
    ProviderUsage,
    _require_project_write_scope,
    _require_provider_version,
    _usage_int,
    compaction_failed_text,
)
from rcp.providers.browser_grant import BrowserGrant
from rcp.providers.turn_fence import COMPACT_COMMAND
from rcp.providers.usage_limits import LimitEvidence, _exhausted_windows, _iso_timestamp

if TYPE_CHECKING:
    from rcp.agents.invocation_broker import ProviderInvocationGate
    from rcp.agents.write_scope import ProviderWriteScope
    from rcp.core.models import HiddenReadScope
    from rcp.providers.agy.auth import AgyAuthentication

_RUNTIME = "agy.stream-json.v1"
_FILE_TOOLS = (
    "view_file",
    "list_dir",
    "find_by_name",
    "grep_search",
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
    "notebook_edit",
)
_WEB_TOOLS = ("search_web", "read_url_content")
_WORK = {"work_auto", "orchestrate"}


def _tools(capability: AgentCapability) -> tuple[str, ...] | None:
    """A narrower launch's tools, pinned through an agy agent; Work keeps agy's own set.

    agy bills a request made through `--agent` as a custom-agent request, so Work and
    orchestrator turns, which are nearly all agy traffic, launch without one.
    """
    if capability in _WORK:
        return None
    return _FILE_TOOLS + _WEB_TOOLS


def _policy_head(
    binary: str, policy: dict[str, object], tools: tuple[str, ...] | None
) -> tuple[list[str], list[str]]:
    """The wrapper options before the provider flags, and the `--agent` pair if any."""
    head = [binary] + (["--rcp-agy-tools", json.dumps(tools)] if tools is not None else [])
    agent = ["--agent", agy_agent_name(policy, tools)] if tools is not None else []
    return head, agent


def _protected_write_denies(scope: ProviderWriteScope | None) -> list[str]:
    if scope is None:
        return []
    own = [
        scope.stage_root,
        scope.workspace_root,
        *scope.memory_roots,
        *([scope.paper_root] if scope.paper_root else []),
    ]
    return [
        f"write_file({root})"
        for root in scope.protected_write_paths
        if not any(PurePosixPath(root) in PurePosixPath(path).parents for path in own)
    ]


def _write_roots(cwd: Path, scope: ProviderWriteScope | None) -> list[str]:
    return list(scope.writable_roots) if scope is not None else [str(cwd)]


def _additional_workspace_args(request: ProviderTurnRequest) -> list[str]:
    if request.capability not in _WORK:
        return []
    roots = _write_roots(request.cwd, request.write_scope)
    return [
        argument
        for root in dict.fromkeys(roots)
        if root != str(request.cwd)
        for argument in ("--add-dir", root)
    ]


def _policy(
    capability: AgentCapability,
    cwd: Path,
    read_dirs: list[Path],
    scope: ProviderWriteScope | None,
    hidden: HiddenReadScope | None,
    gate: ProviderInvocationGate | None = None,
) -> dict[str, object]:
    roots = _write_roots(cwd, scope)
    allow = ["read_url(*)"]
    if capability in _WORK:
        allow += ["command(*)", "read_file(*)"]
        allow += [f"write_file({root})" for root in roots if root != str(cwd)]
    else:
        allow += [f"read_file({root})" for root in dict.fromkeys(map(str, read_dirs))]
    deny = [f"write_file({cwd}/.agents)"]
    deny += _protected_write_denies(scope)
    if hidden is not None:
        deny += [
            f"read_file({root})" for root in (*hidden.hidden_directories, *hidden.hidden_files)
        ]
    if gate is not None and gate.client_path is not None:
        deny.append(f"write_file({PurePosixPath(gate.client_path).parent})")
    return {
        "agentMode": "",
        "toolPermission": "request-review",
        "allowNonWorkspaceAccess": capability in _WORK,
        "enableTerminalSandbox": False,
        "queuedMessages": "queue",
        "artifactReviewPolicy": "always-proceed",
        "permissions": {"allow": allow, "ask": [], "deny": list(dict.fromkeys(deny))},
    }


def _denied_action_error(denied: object) -> ProviderStreamEvent:
    entries = denied if isinstance(denied, list) else []
    actions = sorted(
        {
            item["action"]
            for item in entries
            if isinstance(item, dict) and isinstance(item.get("action"), str)
        }
    )
    kinds = ", ".join(actions) or "an unspecified action"
    return ProviderStreamEvent(event="error", text=f"Antigravity refused {kinds}.")


def _decode_result(result: object) -> ProviderStreamEvent:
    if not isinstance(result, dict):
        return ProviderStreamEvent(event="error", text="Invalid Antigravity result")
    denied = result.get("denied_actions")
    if denied:
        return _denied_action_error(denied)
    if result.get("status") != "SUCCESS":
        return ProviderStreamEvent(
            event="error",
            text=str(
                result.get("error") or result.get("finished_reason") or "Antigravity turn failed"
            ),
        )
    return ProviderStreamEvent(event="answer", text=str(result.get("response") or ""))


class _AgyStreamTurn(ProviderTurn):
    close_input_after_initial = False
    requires_protocol_completion = True
    steer_delivered_on_write = True

    def __init__(self, profile: AgyProfile, request: ProviderTurnRequest) -> None:
        self.profile = profile
        self.prompt = request.prompt
        self.command = profile._command(request)
        if request.autocompact:
            self.environment = {"AGY_CLI_AUTO_COMPACT_OVERRIDE": request.autocompact}
        self.ids = [str(uuid.uuid4())]
        self.results = 0
        self.ready = False
        self.completed = False
        self.compacting = request.compact_first
        self._held_text: tuple[object, str] | None = None

    def initial_input(self) -> bytes:
        return self._input(COMPACT_COMMAND if self.compacting else self.prompt)

    @staticmethod
    def _input(text: str) -> bytes:
        return (json.dumps({"event": "user", "message": {"content": text}}) + "\n").encode()

    def steering_state(self) -> ProviderSteeringState:
        can_steer = self.ready and not self.completed and not self.compacting
        return ProviderSteeringState(can_steer, turn_id=self.ids[0])

    def render_steer(self, expected_turn_id: str, message_id: str, text: str) -> bytes:
        if not self.steering_state().can_steer or expected_turn_id != self.ids[0]:
            raise ValueError("The addressed provider turn is not active")
        if message_id in self.ids:
            raise ValueError("The message was already sent")
        self.ids.append(message_id)
        return self._input(text)

    def adopt_recorded_inputs(
        self, message_ids: list[str], steer_requests: dict[str, object]
    ) -> None:
        if self.compacting:
            message_ids = message_ids[1:]
        if message_ids:
            self.ids = list(message_ids)

    def receive_line(self, line: str) -> ProviderRuntimeStep:
        if self.completed:
            return ProviderRuntimeStep()
        try:
            value = json.loads(line)
        except ValueError:
            return ProviderRuntimeStep(events=(ProviderStreamEvent(event="raw", text=line),))
        event = self.profile.decode_event(value, line)
        if isinstance(value, dict):
            released = self._hold_text(value)
            if released:
                event = replace(
                    event,
                    status=(event.status or AgentStatusDelta()).model_copy(
                        update={"message": released}
                    ),
                )
            if value.get("event") == "init":
                self.ready = True
            if value.get("event") == "result" and self.compacting:
                return self._compacted(event)
            if value.get("event") == "result":
                self.results += 1
                self.completed = event.event == "error" or self.results >= len(self.ids)
        if event.event == "error":
            self.last_error = event.text
        return ProviderRuntimeStep(
            events=(event,),
            complete=self.completed,
            explicit_terminal=self.completed,
            stop_process=event.event == "error",
        )

    def _compacted(self, event: ProviderStreamEvent) -> ProviderRuntimeStep:
        """The compaction input's result: write the held prompt, or fail without it."""

        self.compacting = False
        if event.event == "error":
            self.completed = True
            self.last_error = compaction_failed_text(event.text)
            return ProviderRuntimeStep(
                events=(replace(event, text=self.last_error),),
                complete=True,
                explicit_terminal=True,
                stop_process=True,
            )
        compacted = AgentStatusDelta(compaction=AgentCompaction(trigger="manual"))
        return ProviderRuntimeStep(
            outgoing=(self._input(self.prompt),),
            events=(ProviderStreamEvent(event="raw", status=compacted),),
        )

    def _hold_text(self, value: dict[str, object]) -> str | None:
        """Response text is an in-between message once the agent acts after it; held text answers.

        Antigravity streams one response step as text deltas sharing its step index.
        """

        if value.get("event") == "result":
            self._held_text = None
            return None
        step = value.get("step_update")
        if value.get("event") != "step_update" or not isinstance(step, dict):
            return None
        held = self._held_text
        released: str | None = None
        if step.get("step_type") == "agent_response":
            index = step.get("step_index")
            if held is not None and held[0] != index:
                released, held = held[1], None
            delta = step.get("text_delta")
            if isinstance(delta, str) and delta:
                held = (index, f"{held[1]}{delta}" if held else delta)
        elif held is not None:
            released, held = held[1], None
        self._held_text = held
        return released if released and released.strip() else None


class _AgyStreamRuntime(ProviderRuntime):
    id = _RUNTIME
    steering_behavior = "queue"
    supports_compact_first = True

    def turn(self, request: ProviderTurnRequest) -> ProviderTurn:
        return _AgyStreamTurn(AgyProfile(), request)


_AUTOCOMPACT_MIN_TOKENS = 30_000


class AgyProfile(ProviderProfile):
    id = "agy"
    label = "Antigravity"
    install_paths = (".local/bin/agy",)
    native_update = ProviderNativeUpdate(self_update_args=("update",))
    legacy_runtime_id = _RUNTIME
    default_runtime = "stream-json"
    runtime_aliases = {"stream-json": _RUNTIME, _RUNTIME: _RUNTIME}
    runtime_choices = (ProviderRuntimeChoice(id="stream-json", label="Antigravity stream JSON"),)
    turn_fences = {_RUNTIME: AgyStreamTurnFence}
    usage_profile = "agy.step.v1"
    work_like_minimum_version = (1, 5, 8)
    shell_timeout_default_seconds = None
    autocompact_hint = f"{_AUTOCOMPACT_MIN_TOKENS} or more tokens"
    hidden_read_gaps = ("shell_wrapper", "glob_rules")
    resume_keeps_tools = True
    login_credential_files = ("agy-home/.gemini/antigravity-cli/antigravity-oauth-token",)
    remote_login_credential_files = (
        ".rcp/provider-homes/agy/{login_id}/.gemini/antigravity-cli/antigravity-oauth-token",
    )
    login_home_credentials = (".gemini/antigravity-cli/antigravity-oauth-token",)

    def limit_text_reset(self, error: str, failed_at: datetime) -> datetime | None:
        match = re.search(r"resets in\s+(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", error, re.I)
        if match and any(match.groups()):
            hours, minutes, seconds = (int(value or 0) for value in match.groups())
            return failed_at + timedelta(hours=hours, minutes=minutes, seconds=seconds)
        return None

    def limit_evidence(self, snapshot: dict[str, object]) -> LimitEvidence:
        groups = snapshot.get("groups")
        if not isinstance(groups, list):
            raise ValueError("Antigravity usage has no groups list")
        windows: list[tuple[bool, datetime | None]] = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("buckets"), list):
                raise ValueError("Antigravity usage group has no buckets list")
            for bucket in group["buckets"]:
                remaining = bucket.get("remaining_fraction") if isinstance(bucket, dict) else None
                if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
                    raise ValueError("Antigravity usage bucket has no remaining_fraction")
                windows.append((remaining <= 0, _iso_timestamp(bucket.get("reset_time"))))
        return _exhausted_windows(windows)

    @property
    def authentication(self) -> AgyAuthentication:
        from rcp.providers.agy.auth import AgyAuthentication

        return AgyAuthentication()

    def runtime(self, runtime_id: str) -> ProviderRuntime:
        if runtime_id == _RUNTIME:
            return _AgyStreamRuntime()
        return super().runtime(runtime_id)

    def validate_readiness_version(
        self, actual: str | None, *, capability: AgentCapability
    ) -> None:
        _require_provider_version(provider=self.label, actual=actual, minimum=(1, 5, 8))

    def auth_command(self, binary: str) -> list[str]:
        return [binary, "models"]

    def catalog_command(self, binary: str) -> list[str]:
        return self.auth_command(binary)

    def login_command(self, binary: str) -> list[str]:
        return [binary, "acp", "--login"]

    def parse_limit_usage(self, stdout: str) -> dict[str, object]:
        payload = json.loads(stdout)
        command = payload.get("command") if isinstance(payload, dict) else None
        data = command.get("data") if isinstance(command, dict) else None
        if not isinstance(data, dict):
            raise ValueError("Antigravity usage has no command.data")
        self.limit_evidence(data)
        return data

    @property
    def limit_probe(self) -> Callable[[str], ProviderLimitProbe]:
        return self._limit_probe

    def _limit_probe(self, binary: str) -> ProviderLimitProbe:
        return ProviderLimitProbe(self.login_probe_command(binary), "", self.parse_limit_usage)

    def login_probe_command(self, binary: str) -> list[str]:
        return [binary, "-p", "/usage", "--output-format", "json"]

    def is_authenticated(self, result: subprocess.CompletedProcess[str]) -> bool:
        return result.returncode == 0

    def parse_catalog(self, stdout: str) -> list[ModelChoice]:
        return [
            ModelChoice(id=slug, label=label)
            for line in stdout.splitlines()
            if "\t" in line
            for slug, label in [line.split("\t", 1)]
            if slug and label
        ]

    def skill_probe(self, binary: str) -> ProviderSkillProbe:
        return ProviderSkillProbe(
            command=[binary, "-p", "/skills", "--output-format", "json"],
            protocol="jsonl",
        )

    def parse_skills(self, payload: object) -> list[ProviderSkill]:
        value = json.loads(payload) if isinstance(payload, str) else payload
        if not isinstance(value, dict):
            raise ValueError("Invalid Antigravity skill inventory")
        return [
            ProviderSkill(
                name=entry["name"],
                label=entry["name"],
                description=entry.get("description", ""),
                path=entry.get("path"),
                scope="built-in" if entry.get("builtin") else None,
            )
            for entry in value.get("command", {}).get("data", {}).get("skills", [])
            if entry.get("model_invocable", True)
        ]

    def work_like_probe_command(self, binary: str) -> list[str]:
        policy = _policy("work_auto", Path.cwd(), [], None, None)
        return [
            binary,
            "--rcp-agy-probe",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--settings",
            json.dumps(policy),
            "--mode",
            "accept-edits",
        ]

    def canonical_autocompact(self, value: str) -> str:
        value = value.strip()
        if value.isascii() and value.isdecimal() and int(value) >= _AUTOCOMPACT_MIN_TOKENS:
            return str(int(value))
        raise ValueError(f"Antigravity auto-compact must be {self.autocompact_hint}")

    def project_write_enforcement_mode(self) -> str:
        return "agy.permission-rules.v1"

    def canonical_state_boundary(self) -> str:
        return "file_tools_only"

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
        write_scope: ProviderWriteScope | None,
        capability: AgentCapability,
        provider_version: str | None,
        browser_grant: BrowserGrant | None = None,
        hidden_read_scope: HiddenReadScope | None = None,
        autocompact: str = "",
        fork_session_id: str | None = None,
    ) -> list[str]:
        return self._command(
            ProviderTurnRequest(
                prompt=prompt,
                binary=binary,
                cwd=cwd,
                model=model,
                reasoning=reasoning,
                session_id=session_id,
                read_dirs=read_dirs,
                write_dirs=write_dirs,
                write_scope=write_scope,
                capability=capability,
                provider_version=provider_version,
                browser_grant=browser_grant,
                hidden_read_scope=hidden_read_scope,
                autocompact=autocompact,
                fork_session_id=fork_session_id,
            )
        )

    def _command(self, request: ProviderTurnRequest) -> list[str]:
        if request.capability == "session_summary":
            raise ValueError("Antigravity does not support read-only session summaries")
        if request.fork_session_id:
            raise ValueError("Antigravity does not support native session forks")
        if request.capability in {"work_auto", "orchestrate"}:
            _require_project_write_scope(
                request.write_scope, capability=request.capability, write_dirs=request.write_dirs
            )
        elif request.write_scope is not None:
            raise ValueError("This capability cannot carry a project write scope")
        self.validate_readiness_version(request.provider_version, capability=request.capability)
        policy = _policy(
            request.capability,
            request.cwd,
            request.read_dirs,
            request.write_scope,
            request.hidden_read_scope,
            request.invocation_gate,
        )
        head, agent = _policy_head(request.binary, policy, _tools(request.capability))
        command = [
            *head,
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            *agent,
            "--settings",
            json.dumps(policy, separators=(",", ":")),
            "--mode",
            "accept-edits",
        ]
        if request.model:
            command += ["--model", request.model]
        if request.session_id:
            command += ["--conversation", request.session_id]
        command.extend(_additional_workspace_args(request))
        return command

    def decode_event(self, value: object, raw: str) -> ProviderStreamEvent:
        if not isinstance(value, dict):
            return ProviderStreamEvent(event="raw", text=raw)
        kind = value.get("event")
        if kind == "init":
            return ProviderStreamEvent(event="session", session_id=value.get("conversation_id"))
        if kind == "result":
            return _decode_result(value.get("result"))
        step = value.get("step_update")
        if not isinstance(step, dict):
            return ProviderStreamEvent(event="raw", text=raw)
        if kind == "step_update" and step.get("step_type") in {"agent_response", "checkpoint"}:
            usage = self.decode_usage(value, raw)
            return ProviderStreamEvent(
                event="raw",
                text=raw,
                usage=usage,
                status=AgentStatusDelta(context_tokens=usage.processed_input_tokens)
                if usage and step.get("step_type") == "agent_response"
                else None,
            )
        return ProviderStreamEvent(event="raw", text=raw)

    def decode_usage(self, value: dict[str, object], raw: str) -> ProviderUsage | None:
        step = value.get("step_update")
        if (
            value.get("event") != "step_update"
            or not isinstance(step, dict)
            or step.get("step_type") not in {"agent_response", "checkpoint"}
            or step.get("state") != "DONE"
        ):
            return None
        usage = step.get("usage")
        if not isinstance(usage, dict):
            return None
        incoming = _usage_int(usage.get("input_tokens"))
        cached = _usage_int(usage.get("cache_read_tokens"))
        outgoing = _usage_int(usage.get("output_tokens"))
        return ProviderUsage(
            provider_profile=self.usage_profile,
            provider_event_type=str(step.get("step_type")),
            dedupe_key=f"{step.get('conversation_id')}:{step.get('step_index')}",
            processed_input_tokens=incoming + cached,
            generated_tokens=outgoing,
            cached_input_tokens=cached,
            reasoning_output_tokens=_usage_int(usage.get("thinking_tokens")),
            reported_input_tokens=incoming,
            reported_output_tokens=outgoing,
            reported_total_tokens=_usage_int(usage.get("total_tokens")),
            provider_fields=usage,
        )
