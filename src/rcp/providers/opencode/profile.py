"""Everything RCP knows about launching, containing, and decoding OpenCode.

Probed with OpenCode 1.18.30 on 2026-09-28. `opencode run` reads the prompt on
stdin and prints one JSON event per line. It takes its policy from the
`OPENCODE_CONFIG_CONTENT` environment variable, merged last over the user's
own configuration, so each turn carries its rules as a variable.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
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
    ProviderStreamEvent,
    ProviderTurn,
    ProviderTurnRequest,
    ProviderUsage,
    _JsonlProviderTurn,
    _optional_usage_int,
    _require_project_write_scope,
    _require_provider_version,
    _usage_dedupe_key,
    _usage_int,
)
from rcp.providers.opencode.remote import EXIT_MARKER, OpenCodeRunTurnFence

if TYPE_CHECKING:
    from rcp.agents.write_scope import ProjectWriteScope

_RUNTIME_ID = "opencode.run-json.v1"
# `opencode models --verbose` prints each model as its `provider/model` slug on
# one line, followed by that model's JSON object.
_CATALOG_ENTRY = re.compile(r"^(\S+/\S+)\n\{", re.MULTILINE)
# Edit rules are matched against paths relative to OpenCode's project root:
# the enclosing Git work tree, or `/` outside one. RCP writes them for `/`, so a
# launch carrying them refuses to start inside Git.
_OUTSIDE_GIT_GUARD = (
    "if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then "
    "echo 'RCP cannot bound OpenCode edits from inside a Git work tree.' >&2; exit 64; "
    "fi; "
)
# OpenCode prints no event when a turn ends; a step that stops can be followed
# by another. Its exit is the end, so the wrapper reports it as the last line.
_REPORT_EXIT = (
    f'"$0" "$@"; code=$?; printf \'{{"type":"{EXIT_MARKER}","code":%d}}\\n\' "$code"; exit "$code"'
)


class _OpenCodeRunTurn(_JsonlProviderTurn):
    """One `opencode run` process, ended by the wrapper's report of its exit."""

    def __init__(self, profile: OpenCodeProfile, request: ProviderTurnRequest) -> None:
        super().__init__(profile, request)
        self.environment = profile.launch_environment(request)
        self._session_seen = False
        self._completed = False

    def receive_line(self, line: str) -> ProviderRuntimeStep:
        if self._completed:
            return ProviderRuntimeStep()
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            return ProviderRuntimeStep(events=(ProviderStreamEvent(event="raw", text=line),))
        if not isinstance(value, dict):
            return ProviderRuntimeStep(events=(ProviderStreamEvent(event="raw", text=line),))
        events: list[ProviderStreamEvent] = []
        session_id = value.get("sessionID")
        if not self._session_seen and isinstance(session_id, str) and session_id:
            self._session_seen = True
            events.append(ProviderStreamEvent(event="session", session_id=session_id))
        event = self._profile.decode_event(value, line)
        events.append(event)
        if event.event == "error":
            self.last_error = event.text
        if event.event == "error" or value.get("type") == EXIT_MARKER:
            self._completed = True
            return ProviderRuntimeStep(events=tuple(events), complete=True, explicit_terminal=True)
        return ProviderRuntimeStep(events=tuple(events))


class _OpenCodeRunRuntime(ProviderRuntime):
    id = _RUNTIME_ID

    def turn(self, request: ProviderTurnRequest) -> ProviderTurn:
        return _OpenCodeRunTurn(OpenCodeProfile(), request)


class OpenCodeProfile(ProviderProfile):
    # The CLI's own login applies: RCP manages no OpenCode credential, and the
    # free OpenCode models need none. No credential failure has been observed.
    id = "opencode"
    label = "OpenCode"
    install_paths = (".opencode/bin/opencode",)
    usage_profile = "opencode.step.v1"
    native_update = ProviderNativeUpdate(self_update_args=("upgrade",))
    legacy_runtime_id = _RUNTIME_ID
    default_runtime = "run-json"
    runtime_aliases = {"run-json": _RUNTIME_ID, _RUNTIME_ID: _RUNTIME_ID}
    turn_fences = {_RUNTIME_ID: OpenCodeRunTurnFence}
    runtime_choices = (ProviderRuntimeChoice(id="run-json", label="OpenCode run JSON"),)
    # The version whose edit-rule matching RCP's rules were probed against.
    work_like_minimum_version = (1, 18, 30)

    def validate_readiness_version(
        self,
        actual: str | None,
        *,
        capability: AgentCapability,
    ) -> None:
        # Every capability, not only Work, relies on the probed rule matching.
        del capability
        _require_provider_version(
            provider=self.label, actual=actual, minimum=self.work_like_minimum_version
        )

    def runtime(self, runtime_id: str) -> ProviderRuntime:
        if runtime_id == self.legacy_runtime_id:
            return _OpenCodeRunRuntime()
        return super().runtime(runtime_id)

    def auth_command(self, binary: str) -> list[str]:
        return [binary, "providers", "list"]

    def login_command(self, binary: str) -> list[str]:
        return [binary, "providers", "login"]

    def is_authenticated(self, result: subprocess.CompletedProcess[str]) -> bool:
        # OpenCode serves its own free models without any stored credential, so
        # a CLI that answers is worth launching.
        return result.returncode == 0

    def catalog_command(self, binary: str) -> list[str] | None:
        return [binary, "models", "--verbose"]

    def parse_catalog(self, stdout: str) -> list[ModelChoice]:
        decoder = json.JSONDecoder()
        choices: list[ModelChoice] = []
        position = 0
        while match := _CATALOG_ENTRY.search(stdout, position):
            entry, position = decoder.raw_decode(stdout, match.end() - 1)
            if not isinstance(entry, dict) or entry.get("status") not in {None, "active"}:
                continue
            variants = entry.get("variants")
            choices.append(
                ModelChoice(
                    id=match.group(1),
                    label=str(entry.get("name") or match.group(1)),
                    reasoning=list(variants) if isinstance(variants, dict) else [],
                )
            )
        return choices

    def skill_probe(self, binary: str) -> ProviderSkillProbe:
        return ProviderSkillProbe(command=[binary, "debug", "skill"], protocol="jsonl")

    def parse_skills(self, payload: object) -> list[ProviderSkill]:
        if not isinstance(payload, str):
            raise ValueError("OpenCode skill inventory is not text")
        entries = json.loads(payload)
        if not isinstance(entries, list):
            raise ValueError("OpenCode skill inventory is not a list")
        skills: dict[str, ProviderSkill] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                continue
            location = entry.get("location")
            built_in = location == "<built-in>"
            description = entry.get("description")
            skills[name] = ProviderSkill(
                name=name,
                label=name,
                description=description if isinstance(description, str) else "",
                scope="built-in" if built_in else None,
                path=location if isinstance(location, str) and not built_in else None,
            )
        return list(skills.values())

    def native_skill_token(self, name: str) -> str:
        # OpenCode's model loads a skill through its `skill` tool, by name.
        return name

    def project_write_enforcement_mode(self) -> str:
        return "opencode.agent-permission-rules.v1"

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
    ) -> list[str]:
        del prompt, read_dirs
        work_like = capability in {"work_auto", "orchestrate"}
        if work_like:
            _require_project_write_scope(write_scope, capability=capability, write_dirs=write_dirs)
        elif write_scope is not None:
            raise ValueError(f"capability {capability!r} cannot carry a project write scope")
        self.validate_readiness_version(provider_version, capability=capability)
        permission = _permission(capability, cwd, write_dirs, write_scope)
        # `--pure` loads no plugin, whose config hook could rewrite these rules.
        command = [binary, "run", "--format", "json", "--pure", "--agent", _agent_name(permission)]
        if session_id:
            command.extend(["--session", session_id])
        if model:
            command.extend(["--model", model])
        if reasoning:
            command.extend(["--variant", reasoning])
        # The paper coach denies every edit outright, so no rule depends on the base.
        guard = "" if capability == "paper_readonly" else _OUTSIDE_GIT_GUARD
        return ["sh", "-c", guard + _REPORT_EXIT, *command]

    def launch_environment(self, request: ProviderTurnRequest) -> dict[str, str]:
        """The variables that carry this turn's rules to OpenCode."""

        permission = _permission(
            request.capability, request.cwd, request.write_dirs, request.write_scope
        )
        policy = {
            "$schema": "https://opencode.ai/config.json",
            "share": "disabled",
            "autoupdate": False,
            "snapshot": False,
            # Formatters and language servers from the member's config run their
            # own commands on edited files, outside any tool rule.
            "formatter": False,
            "lsp": False,
            # A dedicated agent, because agent rules are evaluated after
            # top-level ones: neither the user's top-level rules nor their own
            # agents can widen these.
            "agent": {_agent_name(permission): {"mode": "primary", "permission": permission}},
        }
        return {
            "OPENCODE_CONFIG_CONTENT": json.dumps(policy, separators=(",", ":")),
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        }

    def decode_event(self, value: object, raw: str) -> ProviderStreamEvent:
        if not isinstance(value, dict):
            return ProviderStreamEvent(event="raw", text=raw)
        kind = value.get("type")
        part = value.get("part") if isinstance(value.get("part"), dict) else {}
        if kind == "error":
            error = value.get("error")
            detail = ""
            if isinstance(error, dict):
                data = error.get("data")
                message = data.get("message") if isinstance(data, dict) else None
                detail = str(message or error.get("name") or "")
            return ProviderStreamEvent(event="error", text=detail or "OpenCode turn failed.")
        text = part.get("text")
        # Each text part is the model speaking to the human; as with Codex's
        # agent messages, the reply is all of them.
        if kind == "text" and isinstance(text, str) and text:
            return ProviderStreamEvent(event="answer", text=text)
        return ProviderStreamEvent(event="raw", text=raw, usage=self.decode_usage(value, raw))

    def decode_usage(self, value: dict[str, object], raw: str) -> ProviderUsage | None:
        # Every step is one model call and reports only its own tokens.
        part = value.get("part")
        if value.get("type") != "step_finish" or not isinstance(part, dict):
            return None
        tokens = part.get("tokens")
        if not isinstance(tokens, dict):
            return None
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        reported_input = _usage_int(tokens.get("input"))
        cache_read = _usage_int(cache.get("read"))
        cache_write = _usage_int(cache.get("write"))
        output = _usage_int(tokens.get("output"))
        return ProviderUsage(
            provider_profile=self.usage_profile,
            provider_event_type="step_finish",
            dedupe_key=_usage_dedupe_key(part, raw, "id"),
            processed_input_tokens=reported_input + cache_read + cache_write,
            generated_tokens=output,
            cached_input_tokens=cache_read,
            cache_write_input_tokens=cache_write,
            reasoning_output_tokens=_usage_int(tokens.get("reasoning")),
            reported_input_tokens=reported_input,
            reported_output_tokens=output,
            reported_total_tokens=_optional_usage_int(tokens.get("total")),
            provider_fields={str(key): item for key, item in tokens.items()},
        )


#: Built-in tools that change nothing, allowed to every launch. Everything else is
#: denied first: `task`, whose subagents, built-in ones included, carry their own
#: rules, and any MCP or custom tool the member's global config adds.
_READ_TOOLS = ("read", "glob", "grep", "list", "skill", "todowrite", "webfetch", "websearch")


def _permission(
    capability: AgentCapability,
    cwd: Path,
    write_dirs: list[Path],
    scope: ProjectWriteScope | None,
) -> dict[str, object]:
    # OpenCode applies the last rule that matches, so the blanket deny comes first.
    base: dict[str, object] = {"*": "deny", **dict.fromkeys(_READ_TOOLS, "allow")}
    # Every capability may read outside its folder.
    base["external_directory"] = "allow"
    if capability == "paper_readonly":
        # Reads its staged inputs outside the project; it can neither edit nor run.
        return base
    if scope is not None:
        allowed, denied = scope.writable_roots, scope.protected_write_paths
    else:
        allowed, denied = [str(cwd), *(str(item) for item in write_dirs)], []
    # Grants, then protected storage, then again each grant inside it, such as
    # a stage in RCP storage.
    edit: dict[str, str] = {"*": "deny"}
    edit.update({_root_pattern(path): "allow" for path in allowed})
    edit.update({_root_pattern(path): "deny" for path in denied})
    for path in allowed:
        if any(PurePosixPath(item) in PurePosixPath(path).parents for item in denied):
            edit.pop(_root_pattern(path))
            edit[_root_pattern(path)] = "allow"
    base["edit"] = edit
    if scope is not None:
        # Only Work runs commands. Nothing bounds the shell's writes, and Discuss
        # and ingestion may write no further than their own folders.
        base["bash"] = "allow"
    return base


def _root_pattern(path: str) -> str:
    """A folder and everything in it, relative to OpenCode's `/` project root."""
    return f"{path.strip('/')}/**"


def _agent_name(permission: dict[str, object]) -> str:
    # Named after its own rules, so no agent a user defines can share the name
    # and have OpenCode merge the two.
    digest = hashlib.sha256(json.dumps(permission, sort_keys=True).encode()).hexdigest()
    return f"rcp-{digest[:16]}"
