from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel

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


def resolve_agent_capability(
    capability: AgentCapability | None,
    *,
    read_only: bool,
) -> AgentCapability:
    """Map the retired read-only switch onto the fixed capability contract.

    Existing callers keep their exact launch behavior while conversation and
    graph-run callers migrate to an explicit capability.
    """

    if capability is not None:
        return capability
    return "paper_readonly" if read_only else "scratch_patch"


class ModelChoice(BaseModel):
    """One model a provider accepts, with the reasoning efforts it supports."""

    id: str
    label: str
    reasoning: list[str] = []
    default_reasoning: str = ""


@dataclass(frozen=True)
class ProviderStreamEvent:
    event: Literal["session", "message", "answer", "error", "raw"]
    text: str = ""
    session_id: str | None = None


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


class CodexProfile(ProviderProfile):
    id = "codex"
    label = "Codex"
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
        if capability == "work_auto":
            writable_roots = ",".join(
                f"{json.dumps(directory)}=true"
                for directory in dict.fromkeys(str(item) for item in write_dirs)
            )
            permission_profile = (
                "permissions={rcp_work={"
                'extends=":workspace",'
                f"workspace_roots={{{writable_roots}}},"
                'filesystem={":workspace_roots"={"."="write",".research"="read"}},'
                'network={enabled=true,domains={"*"="allow"}}'
                "}}"
            )
            command.extend(
                [
                    "--config",
                    'approval_policy="on-request"',
                    "--config",
                    'approvals_reviewer="auto_review"',
                    "--config",
                    'default_permissions="rcp_work"',
                    "--config",
                    permission_profile,
                ]
            )
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
        if event_type in {"thread.started", "session.started"}:
            return ProviderStreamEvent(
                event="session",
                session_id=value.get("thread_id") or value.get("session_id"),
            )
        if event_type in {"turn.failed", "error"}:
            error = value.get("error")
            if isinstance(error, dict):
                detail = error.get("message") or json.dumps(error, ensure_ascii=False)
            else:
                detail = error or value.get("message") or "Codex turn failed."
            return ProviderStreamEvent(event="error", text=str(detail))
        item = value.get("item", {})
        if not isinstance(item, dict):
            item = {}
        text = item.get("text") or value.get("message") or ""
        if text:
            if item.get("type") == "agent_message" and event_type != "item.started":
                return ProviderStreamEvent(event="answer", text=str(text))
            return ProviderStreamEvent(event="message", text=str(text))
        return ProviderStreamEvent(event="raw", text=raw)


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
        # writes. Work therefore uses the same bounded non-interactive edit mode
        # as graph-patch runs. The paper coach remains plan-only. No tool
        # allowlist -- the agent needs Bash, Task, and WebSearch.
        permission_mode = {
            "discuss": "acceptEdits",
            "work_auto": "acceptEdits",
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
        if event_type == "system" and value.get("session_id"):
            return ProviderStreamEvent(
                event="session", session_id=str(value["session_id"])
            )
        result = value.get("result")
        detail = _provider_error_text(value)
        terminal_error = (
            value.get("is_error") is True
            or event_type == "error"
            or "error" in subtype.casefold()
        )
        if terminal_error:
            return ProviderStreamEvent(
                event="error", text=detail or "Claude task failed."
            )
        if isinstance(result, str) and result:
            return ProviderStreamEvent(event="answer", text=result)
        return ProviderStreamEvent(event="raw", text=raw)


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
