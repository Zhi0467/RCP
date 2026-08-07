from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field

"""The provider registry.

Registering an agent CLI means adding one `ProviderProfile` subclass here and
listing it in `PROVIDERS`. Nothing about a provider belongs anywhere else — not
a `("codex", "claude")` tuple, not a display-name ternary, not an option list in
a React component. Two bugs on 2026-07-30 came from provider facts written from
memory into the frontend: a reasoning list that offered a value the models
reject, and a correct control hidden on a false premise about the launch
command.

Where the CLI can enumerate its own models, the profile probes it and RCP offers
exactly what came back. Where it cannot, the profile declares the lists and
records the CLI version they were read from, so the staleness is visible to
whoever maintains them.
"""


AgentCapability = Literal[
    "discuss",
    "work_auto",
    "scratch_patch",
    "paper_readonly",
]


class ModelChoice(BaseModel):
    """One model a provider accepts, with the reasoning efforts it supports."""

    id: str
    label: str
    reasoning: list[str] = []
    default_reasoning: str = ""


class ProviderUsage(BaseModel):
    """Provider-normalized usage at one accounting boundary.

    `processed_input_tokens` and `generated_tokens` are the shared accounting
    fields. The reported fields and `provider_fields` preserve what the CLI
    actually emitted, because providers do not use identical cache semantics.
    """

    provider_profile: str
    provider_event_type: str
    dedupe_key: str
    processed_input_tokens: int = Field(ge=0)
    generated_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    cache_write_input_tokens: int = Field(default=0, ge=0)
    reasoning_output_tokens: int = Field(default=0, ge=0)
    reported_input_tokens: int | None = Field(default=None, ge=0)
    reported_output_tokens: int | None = Field(default=None, ge=0)
    reported_total_tokens: int | None = Field(default=None, ge=0)
    provider_fields: dict[str, object] = Field(default_factory=dict)


@dataclass(frozen=True)
class ProviderStreamEvent:
    event: Literal["session", "message", "answer", "error", "raw"]
    text: str = ""
    session_id: str | None = None
    usage: ProviderUsage | None = None


class ProviderProfile:
    """Everything RCP knows about one agent CLI."""

    id: str
    label: str
    #: The CLI version `declared` was last verified against. Empty when the
    #: profile probes the CLI instead of declaring, which cannot go stale.
    declared_against: str = ""
    #: Models known without asking the CLI. Ignored when `catalog_command`
    #: returns a command that answers.
    declared: tuple[ModelChoice, ...] = ()
    local_session_roots_field: str
    remote_session_roots_field: str
    usage_profile: str = "unknown.v1"

    def session_roots(self, sources: object, *, remote: bool) -> list[str]:
        """Return this provider's configured native-session roots.

        Keeping the mapping here makes native handoff discovery follow the same
        registry boundary as launch and stream decoding. Adding a provider does
        not require another provider-name branch in the retry assembler.
        """
        field = self.remote_session_roots_field if remote else self.local_session_roots_field
        roots = getattr(sources, field, None)
        if not isinstance(roots, list) or not all(isinstance(item, str) for item in roots):
            raise ValueError(f"Provider {self.id!r} has no configured session roots")
        return roots

    def auth_command(self, binary: str) -> list[str]:
        """The argv that reports whether this CLI is logged in."""
        raise NotImplementedError

    def is_authenticated(self, result: subprocess.CompletedProcess[str]) -> bool:
        raise NotImplementedError

    def catalog_command(self, binary: str) -> list[str] | None:
        """The argv that enumerates models, or None when the CLI cannot."""
        return None

    def parse_catalog(self, stdout: str) -> list[ModelChoice]:
        return []

    def models(self, catalog: subprocess.CompletedProcess[str] | None) -> list[ModelChoice]:
        """The models to offer, preferring a live catalog over declared ones."""
        if catalog is not None and catalog.returncode == 0:
            try:
                probed = self.parse_catalog(catalog.stdout)
            except (ValueError, KeyError, TypeError):
                probed = []
            if probed:
                return probed
        return list(self.declared)

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
        capability: AgentCapability,
    ) -> list[str]:
        """The argv that runs one turn. `prompt` arrives on stdin."""
        raise NotImplementedError

    def decode_event(self, value: object, raw: str) -> ProviderStreamEvent:
        return ProviderStreamEvent(event="raw", text=raw)

    def decode_usage(self, value: dict[str, object], raw: str) -> ProviderUsage | None:
        return None


class CodexProfile(ProviderProfile):
    id = "codex"
    label = "Codex"
    usage_profile = "codex.turn.v1"
    local_session_roots_field = "codex_roots"
    remote_session_roots_field = "remote_codex_roots"

    def auth_command(self, binary: str) -> list[str]:
        return [binary, "login", "status"]

    def is_authenticated(self, result: subprocess.CompletedProcess[str]) -> bool:
        if result.returncode != 0:
            return False
        # "Not logged in" contains "logged in", so the negative has to be ruled
        # out first. Today a logged-out `codex login status` also exits non-zero,
        # which hid this; that is the CLI's choice to change, not ours to rely on.
        reported = (result.stdout + result.stderr).lower()
        return "not logged in" not in reported and "logged in" in reported

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
        capability: AgentCapability,
    ) -> list[str]:
        del prompt, read_dirs, write_dirs
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
        if capability == "work_auto":
            command.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            command.extend(["--config", 'approval_policy="never"'])
            sandbox = "read-only" if capability == "paper_readonly" else "workspace-write"
            if session_id:
                command.extend(["--config", f'sandbox_mode="{sandbox}"'])
            else:
                command.extend(["--sandbox", sandbox])
            if capability != "paper_readonly":
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
            return ProviderStreamEvent(event="error", text=str(detail), usage=usage)
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


# Claude Code has no `codex debug models` equivalent, so its lists are read by
# hand from `claude --help`, which documents the accepted values of `--effort`
# and the model aliases. Re-read both when bumping the CLI and move
# `ClaudeProfile.declared_against` to the version you read them from.
_CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
_CLAUDE_MODELS = tuple(
    ModelChoice(id=slug, label=label, reasoning=_CLAUDE_EFFORTS, default_reasoning="medium")
    for slug, label in (
        ("opus", "Opus"),
        ("sonnet", "Sonnet"),
        ("haiku", "Haiku"),
        ("fable", "Fable"),
    )
)


class ClaudeProfile(ProviderProfile):
    id = "claude"
    label = "Claude"
    usage_profile = "claude.query.v1"
    local_session_roots_field = "claude_roots"
    remote_session_roots_field = "remote_claude_roots"
    declared_against = "2.1.219"
    declared = _CLAUDE_MODELS

    def auth_command(self, binary: str) -> list[str]:
        return [binary, "auth", "status"]

    def is_authenticated(self, result: subprocess.CompletedProcess[str]) -> bool:
        try:
            return bool(json.loads(result.stdout).get("loggedIn"))
        except (json.JSONDecodeError, AttributeError):
            return False

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
        capability: AgentCapability,
    ) -> list[str]:
        # Claude accepts `auto` syntactically but non-interactive `--print`
        # normalizes it to `default` and denies both scratch and repository
        # writes. Work bypasses permission prompts so it can execute unattended;
        # scratch-patch runs retain acceptEdits and the paper coach remains
        # plan-only. Native public-web retrieval is pre-authorized explicitly
        # on non-bypass launches without broadening Bash permissions.
        permission_mode = {
            "discuss": "acceptEdits",
            "work_auto": "bypassPermissions",
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
        if capability != "work_auto":
            command.extend(["--allowedTools", "WebSearch", "WebFetch"])
        if session_id:
            command.extend(["--resume", session_id])
        # Deduplicate while preserving first-seen order: one --add-dir per source
        # session directory previously blew past the argv size limit.
        for directory in dict.fromkeys(str(item) for item in [*read_dirs, *write_dirs]):
            command.extend(["--add-dir", directory])
        if model:
            command.extend(["--model", model])
        if reasoning:
            command.extend(["--effort", reasoning])
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


def _provider_error_text(value: dict[str, object]) -> str:
    for candidate in (value.get("result"), value.get("error"), value.get("message")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        if isinstance(candidate, dict):
            message = candidate.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
            return json.dumps(candidate, ensure_ascii=False)
    return ""


def _usage_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _optional_usage_int(value: object) -> int | None:
    if value is None:
        return None
    return _usage_int(value)


def _usage_dedupe_key(value: dict[str, object], raw: str, *fields: str) -> str:
    for field in fields:
        candidate = value.get(field)
        if isinstance(candidate, str) and candidate:
            return candidate
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


PROVIDERS: dict[str, ProviderProfile] = {
    profile.id: profile for profile in (CodexProfile(), ClaudeProfile())
}
#: Iteration order for every place that walks all providers.
PROVIDER_IDS: tuple[str, ...] = tuple(PROVIDERS)
DEFAULT_PROVIDER = CodexProfile.id


def classify_terminal_error(text: str) -> str:
    """Classify a persisted provider error without depending on a provider id."""
    folded = " ".join(text.casefold().split())
    if any(
        marker in folded
        for marker in (
            "session limit",
            "usage limit",
            "hit your limit",
            "quota exceeded",
            "out of credits",
            "weighted tokens left",
        )
    ):
        return "session_limit"
    return "provider_error"


def profile_for(provider: str) -> ProviderProfile:
    try:
        return PROVIDERS[provider]
    except KeyError:
        raise ValueError(f"Unknown agent provider: {provider!r}") from None


def _known_provider(value: str) -> str:
    profile_for(value)
    return value


#: A provider id validated against the registry. Replaces the
#: `Literal["claude", "codex"]` that used to be repeated across the schema
#: layer, so adding a provider does not mean editing every model that names one.
ProviderId = Annotated[str, AfterValidator(_known_provider)]
