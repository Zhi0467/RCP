"""Everything RCP knows about launching, containing, and decoding Claude Code."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import uuid
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from rcp.providers.base import (
    AgentCapability,
    ModelChoice,
    ProviderNativeUpdate,
    ProviderProfile,
    ProviderRuntime,
    ProviderRuntimeChoice,
    ProviderRuntimeStep,
    ProviderSkill,
    ProviderSkillProbe,
    ProviderSteeringState,
    ProviderSteerReceipt,
    ProviderStreamEvent,
    ProviderTurn,
    ProviderTurnRequest,
    ProviderUsage,
    _JsonlProviderTurn,
    _optional_usage_int,
    _provider_error_text,
    _require_project_write_scope,
    _usage_dedupe_key,
    _usage_int,
)
from rcp.providers.browser_grant import BrowserGrant
from rcp.providers.claude.remote import (
    ClaudeSessionFormat,
    ClaudeStreamTurnFence,
    attributed_ids,
    is_claude_task_notice,
)

if TYPE_CHECKING:
    from rcp.agents.invocation_broker import ProviderInvocationGate
    from rcp.agents.write_scope import ProjectWriteScope
    from rcp.core.models import HiddenReadScope
    from rcp.provider_auth import ProviderAuthentication


class _ClaudeStreamTurn(_JsonlProviderTurn):
    close_input_after_initial = False
    requires_protocol_completion = True

    def __init__(self, profile: ProviderProfile, request: ProviderTurnRequest) -> None:
        if request.hidden_read_scope is not None and request.legacy_command is None:
            command = profile.command(
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
                autocompact=request.autocompact,
            )
            request = replace(request, legacy_command=command)
        super().__init__(profile, request)
        if request.shell_timeout_seconds is not None:
            seconds = request.shell_timeout_seconds
            index = self.command.index("--settings") + 1 if "--settings" in self.command else None
            settings = json.loads(self.command[index]) if index is not None else {}
            settings.setdefault("env", {}).update(
                BASH_DEFAULT_TIMEOUT_MS=str(seconds * 1000),
                BASH_MAX_TIMEOUT_MS=str(max(seconds, _BASH_MAX_TIMEOUT_DEFAULT_SECONDS) * 1000),
            )
            rendered = json.dumps(settings, separators=(",", ":"))
            if index is None:
                self.command.extend(["--settings", rendered])
            else:
                self.command[index] = rendered
        if (
            request.capability == "discuss"
            and request.invocation_gate is not None
            and request.invocation_gate.client_path is not None
        ):
            self.command = _with_discuss_client_permissions(self.command, request.invocation_gate)
        if (
            request.hidden_read_scope is not None
            and request.hidden_read_scope.account_home is not None
        ):
            self.environment = {
                "CLAUDE_CODE_SHELL_PREFIX": _shell_prefix(request.hidden_read_scope.wrapper_path())
            }
        self.command.extend(["--input-format", "stream-json", "--replay-user-messages"])
        # Claude has no native per-turn precondition. This token identifies only
        # this fresh process's initial input, never a resumable provider session.
        self._turn_id = str(uuid.uuid4())
        self._ready = False
        self._completed = False
        self._pending: set[str] = set()
        self._generated: set[str] = {self._turn_id}
        self._outstanding: set[str] = set()

    @staticmethod
    def _user_input(message_id: str, text: str) -> bytes:
        return (
            json.dumps(
                {
                    "type": "user",
                    "uuid": message_id,
                    "message": {"role": "user", "content": text},
                    "parent_tool_use_id": None,
                    "session_id": "",
                },
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    def initial_input(self) -> bytes:
        return self._user_input(self._turn_id, self._prompt)

    def adopt_recorded_inputs(
        self, message_ids: list[str], steer_requests: dict[str, object]
    ) -> None:
        if not message_ids:
            return
        # The first is the prompt; the rest are steers this turn accepted. A
        # result naming any of them belongs to this turn, and a fresh id would
        # have let the first result end a turn that was still answering.
        self._turn_id = message_ids[0]
        self._generated = set(message_ids)

    def steering_state(self) -> ProviderSteeringState:
        if self._completed:
            return ProviderSteeringState(False, "The provider turn has completed.")
        if not self._ready:
            return ProviderSteeringState(
                False, "Waiting for the provider to acknowledge this turn."
            )
        return ProviderSteeringState(True, turn_id=self._turn_id)

    def render_steer(self, expected_turn_id: str, message_id: str, text: str) -> bytes:
        state = self.steering_state()
        if not state.can_steer:
            raise ValueError(state.reason)
        if expected_turn_id != self._turn_id:
            raise ValueError("The addressed provider turn is no longer active.")
        if message_id in self._generated:
            raise ValueError("This message was already sent to the provider.")
        self._pending.add(message_id)
        self._generated.add(message_id)
        return self._user_input(message_id, text)

    def receive_line(self, line: str) -> ProviderRuntimeStep:
        if self._completed:
            return ProviderRuntimeStep()
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            return super().receive_line(line)
        if isinstance(value, dict):
            if value.get("type") == "command_lifecycle":
                message_id = value.get("command_uuid")
                if not isinstance(message_id, str) or message_id not in self._generated:
                    return ProviderRuntimeStep()
                state = value.get("state")
                if state in {"queued", "started"}:
                    self._outstanding.add(message_id)
                elif state == "completed":
                    self._outstanding.discard(message_id)
                if state == "started" and message_id == self._turn_id:
                    self._ready = True
                if state == "queued" and message_id in self._pending:
                    self._pending.remove(message_id)
                    return ProviderRuntimeStep(
                        steer_receipts=((message_id, ProviderSteerReceipt("delivered")),)
                    )
                return ProviderRuntimeStep()
            if value.get("type") == "user" and value.get("isReplay") is True:
                return ProviderRuntimeStep()
            if value.get("type") == "result":
                finished = attributed_ids(value)
                self._outstanding.difference_update(finished)
                event = self._profile.decode_event(value, line)
                if is_claude_task_notice(value, self._generated, finished):
                    return ProviderRuntimeStep(
                        events=(ProviderStreamEvent(event="raw", text=line, usage=event.usage),)
                    )
                # A failing result ends the invocation even with a follow-up
                # accepted: the task engine stops at the first error anyway, so
                # continuing would only run a turn whose task has already
                # failed. Ending here keeps the error path terminal, which is
                # what the launcher's stderr drain and stop assume.
                if finished and self._outstanding and event.event != "error":
                    return ProviderRuntimeStep(events=(event,))
                self._completed = True
                receipts = tuple(
                    (message_id, ProviderSteerReceipt("refused", "Turn completed before delivery."))
                    for message_id in self._pending
                )
                self._pending.clear()
                return ProviderRuntimeStep(
                    events=(event,),
                    complete=True,
                    explicit_terminal=True,
                    stop_process=True,
                    steer_receipts=receipts,
                )
        return super().receive_line(line)


class _ClaudeStreamRuntime(ProviderRuntime):
    id = "claude.stream-json.v1"
    steering_behavior = "queue"

    def turn(self, request: ProviderTurnRequest) -> ProviderTurn:
        return _ClaudeStreamTurn(ClaudeProfile(), request)


# Claude Code has no `codex debug models` equivalent. The efforts here are the
# fallback for when the `--help` probe below cannot be read; the aliases are the
# real hand-maintained list, because `--help` documents them only by example.
# Re-read the aliases when bumping the CLI and move `ClaudeProfile.declared_against`
# to the version you read them from.
_CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
# The values follow `--effort` in its own description. Refusing to cross a `--`
# keeps a later option's parenthetical from being read as this option's values.
_CLAUDE_EFFORT_HELP = re.compile(r"--effort <[^>]+>(?:(?!--)[^(])*\(([^)]*)\)")
_CLAUDE_EFFORT_VALUE = re.compile(r"[a-z][a-z0-9_-]*")
_CLAUDE_MODELS = tuple(
    ModelChoice(id=slug, label=label, reasoning=_CLAUDE_EFFORTS, default_reasoning="medium")
    for slug, label in (
        ("opus", "Opus"),
        ("sonnet", "Sonnet"),
        ("haiku", "Haiku"),
        ("fable", "Fable"),
    )
)


# The window `claude --autocompact` accepts besides `auto` (probed on Claude Code 2.1.287).
_AUTOCOMPACT_MIN_TOKENS = 100_000
_AUTOCOMPACT_MAX_TOKENS = 1_000_000
# Claude Code's own BASH_MAX_TIMEOUT_MS default (probed on 2.1.291).
_BASH_MAX_TIMEOUT_DEFAULT_SECONDS = 600


class ClaudeProfile(ProviderProfile):
    @property
    def authentication(self) -> ProviderAuthentication:
        from rcp.providers.claude.auth import ClaudeAuthentication

        return ClaudeAuthentication()

    # No credential failure signatures have been observed for Claude.
    id = "claude"
    label = "Claude"
    usage_profile = "claude.query.v1"
    local_session_roots_field = "claude_roots"
    remote_session_roots_field = "remote_claude_roots"
    native_update = ProviderNativeUpdate(self_update_args=("update",))
    session_format = ClaudeSessionFormat()
    autocompact_hint = f"auto, or {_AUTOCOMPACT_MIN_TOKENS} to {_AUTOCOMPACT_MAX_TOKENS} tokens"
    legacy_runtime_id = "claude.stream-json.v1"
    default_runtime = "stream-json"
    runtime_aliases = {
        "stream-json": legacy_runtime_id,
        legacy_runtime_id: legacy_runtime_id,
    }
    turn_fences = {legacy_runtime_id: ClaudeStreamTurnFence}
    runtime_choices = (ProviderRuntimeChoice(id="stream-json", label="Claude stream JSON"),)
    work_like_minimum_version = (2, 1, 233)
    #: Only the model aliases are declared; `declared_against` dates them alone.
    declared_against = "2.1.267"
    declared = _CLAUDE_MODELS

    def probe_failure_evidence(self, result: subprocess.CompletedProcess[str]) -> str:
        diagnostics = [result.stderr]
        for line in result.stdout.split("\n"):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                if result.returncode:
                    diagnostics.append(line)
                continue
            if self.decode_event(value, line).event == "error":
                diagnostics.append(line)
        return "\n".join(diagnostics)

    def credential_failure(self, stderr: str) -> bool:
        # Emitted by this provider's remote environment fence when its managed
        # token disappeared.
        if "RCP managed credential is missing" in stderr:
            return True
        # Observed from Claude Code 2.1.270 on 2026-09-15, verifying a setup
        # token the service rejected: "Failed to authenticate. API Error: 401
        # OAuth access token is invalid." The status code carries the meaning,
        # so the sentence around it may be reworded without breaking this.
        reported = " ".join(stderr.casefold().split())
        return "401" in reported and ("authenticate" in reported or "oauth" in reported)

    def runtime(self, runtime_id: str) -> ProviderRuntime:
        if runtime_id == self.legacy_runtime_id:
            return _ClaudeStreamRuntime()
        return super().runtime(runtime_id)

    def auth_command(self, binary: str) -> list[str]:
        return [binary, "auth", "status"]

    def login_command(self, binary: str) -> list[str]:
        return [binary, "auth", "login"]

    def is_authenticated(self, result: subprocess.CompletedProcess[str]) -> bool:
        try:
            return bool(json.loads(result.stdout).get("loggedIn"))
        except (json.JSONDecodeError, AttributeError):
            return False

    def catalog_command(self, binary: str) -> list[str] | None:
        # Claude Code cannot enumerate its models, but `--help` states the exact
        # values `--effort` accepts. Probing beats copying them by hand.
        return [binary, "--help"]

    def parse_catalog(self, stdout: str) -> list[ModelChoice]:
        """Read the accepted `--effort` values and give them to every alias.

        Claude's efforts are provider-wide: the same values are reported for
        every model, and the CLI rejects an unknown one while parsing arguments,
        before a model is chosen. `--help` wraps the values onto a continuation
        line, so the text is flattened before matching.
        """

        match = _CLAUDE_EFFORT_HELP.search(" ".join(stdout.split()))
        if match is None:
            return []
        efforts = [value.strip() for value in match.group(1).split(",") if value.strip()]
        # A parenthesis moving elsewhere in the description would otherwise turn
        # prose into offered efforts. Nothing but a bare word is an effort.
        if not efforts or not all(_CLAUDE_EFFORT_VALUE.fullmatch(value) for value in efforts):
            return []
        default = "medium" if "medium" in efforts else efforts[0]
        return [
            ModelChoice(
                id=model.id, label=model.label, reasoning=efforts, default_reasoning=default
            )
            for model in _CLAUDE_MODELS
        ]

    def launch_degradation(self, stderr: str, *, requested_reasoning: str | None) -> str | None:
        # Claude warns and runs at its default rather than failing, and its
        # stream reports no effort, so this warning is the only signal that the
        # turn did not run the way the human asked.
        if not requested_reasoning or "Unknown --effort value" not in stderr:
            return None
        return (
            f"Claude ignored the requested reasoning effort {requested_reasoning!r} "
            "and ran at its own default."
        )

    def login_probe_command(self, binary: str) -> list[str]:
        # Flags probed with Claude Code 2.1.270 on 2026-09-14.
        return [
            binary,
            "--print",
            "--model",
            "haiku",
            "--output-format",
            "text",
            "--no-session-persistence",
            "--safe-mode",
            "--tools",
            "",
            "--strict-mcp-config",
            "Reply with OK only.",
        ]

    def work_like_probe_command(self, binary: str) -> list[str]:
        return [
            binary,
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "dontAsk",
            "--input-format",
            "stream-json",
            "--no-session-persistence",
            "--setting-sources",
            "",
            "--settings",
            json.dumps(_claude_write_settings(), separators=(",", ":")),
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
        ]

    def skill_probe(self, binary: str) -> ProviderSkillProbe:
        return ProviderSkillProbe(
            command=[
                binary,
                "--print",
                "/context",
                "--no-session-persistence",
                "--output-format",
                "stream-json",
                "--verbose",
                "--permission-mode",
                "plan",
                "--settings",
                '{"disableAllHooks":true}',
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
            ],
            protocol="jsonl",
        )

    def parse_skills(self, payload: object) -> list[ProviderSkill]:
        if not isinstance(payload, str):
            raise ValueError("Claude skill inventory is not JSONL text")
        init: dict[str, object] | None = None
        for line in payload.split("\n"):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(value, dict)
                and value.get("type") == "system"
                and value.get("subtype") == "init"
            ):
                init = value
                break
        if init is None or not isinstance(init.get("skills"), list):
            raise ValueError("Claude system/init has no skills list")
        return [
            ProviderSkill(
                name=name,
                label=name,
                description="Claude-native skill loaded by this CLI.",
                scope="plugin" if ":" in name else None,
            )
            for name in dict.fromkeys(init["skills"])
            if isinstance(name, str) and name
        ]

    def project_write_enforcement_mode(self) -> str:
        return "claude.permission-allowlist.v1"

    def canonical_autocompact(self, value: str) -> str:
        value = value.strip().lower()
        if value == "auto":
            return value
        if value.isdigit() and _AUTOCOMPACT_MIN_TOKENS <= int(value) <= _AUTOCOMPACT_MAX_TOKENS:
            return str(int(value))
        raise ValueError(f"Claude auto-compact must be {self.autocompact_hint}")

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
        autocompact: str = "",
    ) -> list[str]:
        # Claude accepts `auto` syntactically but non-interactive `--print`
        # normalizes it to `default` and denies both scratch and repository
        # writes. Work bypasses permission prompts so it can execute unattended;
        # scratch-patch runs retain acceptEdits and the paper coach remains
        # plan-only. Native public-web retrieval is pre-authorized explicitly
        # on non-bypass launches without broadening Bash permissions.
        work_like = capability in {"work_auto", "orchestrate"}
        scope = None
        if work_like:
            scope = _require_project_write_scope(
                write_scope,
                capability=capability,
                write_dirs=write_dirs,
            )
            self.validate_readiness_version(provider_version, capability=capability)
        elif write_scope is not None:
            raise ValueError(f"capability {capability!r} cannot carry a project write scope")
        permission_mode = {
            "discuss": "acceptEdits",
            "work_auto": "dontAsk",
            "orchestrate": "dontAsk",
            "scratch_patch": "acceptEdits",
            "paper_readonly": "plan",
        }[capability]
        command = [
            binary,
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            permission_mode,
        ]
        if scope is not None:
            command.extend(
                [
                    "--setting-sources",
                    "",
                    "--settings",
                    json.dumps(
                        _claude_write_settings(scope, hidden_read_scope, cwd), separators=(",", ":")
                    ),
                ]
            )
        else:
            if hidden_read_scope is not None:
                command.extend(
                    [
                        "--settings",
                        json.dumps(
                            _claude_hidden_read_settings(hidden_read_scope),
                            separators=(",", ":"),
                        ),
                    ]
                )
            command.extend(["--allowedTools", "WebSearch", "WebFetch"])
            if (
                capability == "discuss"
                and browser_grant is not None
                and browser_grant.status == "granted"
            ):
                command.append("Bash(playwright-cli:*)")
        command.extend(["--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}'])
        if session_id:
            command.extend(["--resume", session_id])
        # Deduplicate while preserving first-seen order: one --add-dir per source
        # session directory previously blew past the argv size limit.
        additional_dirs = [*read_dirs, *write_dirs]
        if scope is not None:
            additional_dirs = [*read_dirs, *(Path(item) for item in scope.repository_roots)]
        for directory in dict.fromkeys(str(item) for item in additional_dirs):
            command.extend(["--add-dir", directory])
        if model:
            command.extend(["--model", model])
        if reasoning:
            command.extend(["--effort", reasoning])
        if autocompact:
            command.extend(["--autocompact", autocompact])
        return command

    def decode_event(self, value: object, raw: str) -> ProviderStreamEvent:
        if not isinstance(value, dict):
            return ProviderStreamEvent(event="raw", text=raw)
        event_type = str(value.get("type") or "")
        subtype = str(value.get("subtype") or "")
        usage = self.decode_usage(value, raw)
        if event_type == "system" and value.get("session_id"):
            return ProviderStreamEvent(
                event="session",
                session_id=str(value["session_id"]),
                usage=usage,
            )
        result = value.get("result")
        detail = _provider_error_text(value)
        terminal_error = (
            value.get("is_error") is True or event_type == "error" or "error" in subtype.casefold()
        )
        if terminal_error:
            return ProviderStreamEvent(
                event="error",
                text=detail or "Claude task failed.",
                usage=usage,
            )
        if isinstance(result, str) and result:
            return ProviderStreamEvent(event="answer", text=result, usage=usage)
        return ProviderStreamEvent(event="raw", text=raw, usage=usage)

    def decode_usage(self, value: dict[str, object], raw: str) -> ProviderUsage | None:
        # Claude's final result is the query-level accounting boundary. Earlier
        # assistant messages may carry step usage and must not be added again.
        if value.get("type") != "result":
            return None
        usage = value.get("usage")
        if not isinstance(usage, dict):
            return None
        reported_input = _usage_int(usage.get("input_tokens"))
        cache_creation = _usage_int(usage.get("cache_creation_input_tokens"))
        cache_read = _usage_int(usage.get("cache_read_input_tokens"))
        reported_output = _usage_int(usage.get("output_tokens"))
        details = usage.get("output_tokens_details")
        thinking = (
            _usage_int(details.get("thinking_tokens"))
            if isinstance(details, dict)
            else _usage_int(usage.get("thinking_tokens"))
        )
        return ProviderUsage(
            provider_profile=self.usage_profile,
            provider_event_type="result",
            dedupe_key=_usage_dedupe_key(value, raw, "message_id", "uuid", "id"),
            processed_input_tokens=reported_input + cache_creation + cache_read,
            generated_tokens=reported_output,
            cached_input_tokens=cache_read,
            cache_creation_input_tokens=cache_creation,
            reasoning_output_tokens=thinking,
            reported_input_tokens=reported_input,
            reported_output_tokens=reported_output,
            reported_total_tokens=_optional_usage_int(usage.get("total_tokens")),
            provider_fields={str(key): item for key, item in usage.items()},
        )


def _claude_write_settings(
    scope: ProjectWriteScope | None = None,
    hidden_read_scope: HiddenReadScope | None = None,
    cwd: Path | None = None,
) -> dict[str, object]:
    # Readiness validates these same settings with no project write authority.
    # Claude's OS sandbox stays off. Its Linux backend always unshares the
    # network namespace and remounts a minimal `/dev`, so a sandboxed Work turn
    # cannot reach a scheduler, a GPU device, or any non-HTTP service on its own
    # execution host. Exact write roots are enforced by Claude's file-permission
    # rules instead: they bound every file-editing tool, and Bash is not bounded.
    writable_roots = scope.writable_roots if scope is not None else []
    protected_write_paths = scope.protected_write_paths if scope is not None else []
    if scope is not None:
        # Claude's deny beats its allow, so a protected folder holding this
        # launch's own stage (RCP storage inside a grant) stays undenied.
        own = [PurePosixPath(scope.stage_root), PurePosixPath(scope.workspace_root)]
        protected_write_paths = [
            path
            for path in protected_write_paths
            if not any(PurePosixPath(path) in item.parents for item in own)
        ]
    # `Edit(path)` is the only file permission rule Claude matches, and it covers
    # every file-editing tool. A `Write(path)` rule is accepted and then ignored.
    allow_patterns = [f"Edit({_claude_absolute_pattern(path)})" for path in writable_roots]
    deny_patterns = [f"Edit({_claude_absolute_pattern(path)})" for path in protected_write_paths]
    settings = {
        "disableAllHooks": True,
        "permissions": {
            "defaultMode": "dontAsk",
            "disableAutoMode": "disable",
            "disableBypassPermissionsMode": "disable",
            "allow": ["Bash", "WebSearch", "WebFetch", *allow_patterns],
            "deny": deny_patterns,
        },
        "sandbox": {"enabled": False},
    }

    if hidden_read_scope is not None:
        hidden = _claude_hidden_read_settings(hidden_read_scope)
        settings["env"] = hidden["env"]
        settings["permissions"]["deny"].extend(hidden["permissions"]["deny"])
    return settings


def _shell_prefix(wrapper: str) -> str:
    # Claude runs the prefix as one executable path with the command as its
    # argument; the executable wrapper reads the policy staged beside it.
    return wrapper


def _claude_hidden_read_settings(scope: HiddenReadScope) -> dict[str, object]:
    patterns = [
        *(_claude_absolute_pattern(path) for path in scope.hidden_directories),
        *(_claude_absolute_pattern(path, directory=False) for path in scope.hidden_files),
        *(_claude_absolute_pattern(path, directory=False) for path in scope.hidden_globs),
    ]
    deny = [f"Read({pattern})" for pattern in patterns]
    if scope.account_home is None:
        return {"env": {}, "permissions": {"deny": deny}}
    wrapper = scope.wrapper_path()
    # A granted root may contain the wrapper's home; its policy stays uneditable.
    deny.append(f"Edit({_claude_absolute_pattern(str(PurePosixPath(wrapper).parent))})")
    return {
        "env": {"CLAUDE_CODE_SHELL_PREFIX": _shell_prefix(wrapper)},
        "permissions": {"deny": deny},
    }


def _claude_absolute_pattern(path: str, *, directory: bool = True) -> str:
    return f"//{path.lstrip('/')}" + ("/**" if directory else "")


def _with_discuss_client_permissions(command: list[str], gate: ProviderInvocationGate) -> list[str]:
    """Grant the isolated client only after denying replacement of its inputs."""
    assert gate.client_path is not None
    command = list(command)
    client = shlex.join(gate.client_executable_argv())
    # Keep the rule inside the existing allowedTools group.
    end = command.index("--allowedTools") + 1
    while end < len(command) and not command[end].startswith("--"):
        end += 1
    command.insert(end, f"Bash({client}:*)")
    settings_index = command.index("--settings") + 1 if "--settings" in command else None
    settings = json.loads(command[settings_index]) if settings_index is not None else {}
    # A literal path, unlike hidden_globs. Escape only the class brackets, which could
    # fail to match their own characters; `*` and `?` still match themselves. A
    # backslash becomes `?`, which matches it: over-denying is the safe side, and
    # Claude's matcher mishandles escaped `?` and backslash pairs.
    directory = re.sub(
        r"([\[\]])", r"\\\1", str(PurePosixPath(gate.client_path).parent).replace("\\", "?")
    )
    settings.setdefault("permissions", {}).setdefault("deny", []).extend(
        f"Edit({_claude_absolute_pattern(directory, directory=descendants)})"
        for descendants in (False, True)
    )
    rendered = json.dumps(settings, separators=(",", ":"))
    if settings_index is None:
        command.extend(["--settings", rendered])
    else:
        command[settings_index] = rendered
    return command
