"""What every owner needs to settle a pass it stopped watching.

Three things, none of them policy. Reading a decoded pass into an outcome, so
the recorded path reaches a verdict the same way the live loop does. Building
the request shape the decoder wants. And reopening the exact stage a launch
named, which is arithmetic plus refusal -- the owner still decides what its
deliverables mean.

Owners keep their own launch snapshot under their own contract role. This module
deliberately holds no snapshot type: a shared one would grow a field per owner,
and the role is what routes a recorded pass back to whoever wrote it.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from rcp.agents import AgentEvent
from rcp.providers import ProviderTurnRequest
from rcp.runs.shared import _ProviderOutcome, _sse
from rcp.transport import RemoteRunStage

if TYPE_CHECKING:
    from rcp.background import AgentTaskExecution
    from rcp.runs.recorded_turn import RecordedProviderTurn, RecordedVerdict


def provider_turn_request(
    workspace: Path,
    recorded: RecordedProviderTurn,
) -> ProviderTurnRequest:
    """The request shape the decoder needs, from what the pass already knows."""

    return ProviderTurnRequest(
        prompt="",
        binary=recorded.provider,
        cwd=workspace,
        model=None,
        reasoning=None,
        session_id=recorded.session_id,
        read_dirs=[],
        write_dirs=[],
        write_scope=None,
        # Nothing launches from this request; it exists so the decoder can build
        # the runtime that reads the wire. Naming the turn's real capability here
        # would ask for a write scope no reader needs and no recovery has.
        capability="paper_readonly",
        provider_version=recorded.provider_version,
    )


def write_recorded_patch(
    workspace: Path,
    remote_stage: RemoteRunStage | None,
    recorded: RecordedProviderTurn,
) -> None:
    """Put the digest-verified deliverables back, before anything reads them.

    A stage is mutable and a recorded pass is not. Settling from whatever the
    directory holds now would let a Patch that changed after the host finished
    be admitted as this turn's, or let a deleted one silently become no Patch at
    all -- while the answer and the rest of the turn are still accepted. The
    watcher handoff is the other half of the same admission and gets the same
    treatment, except where the host never snapshotted it: a journal written
    before supervisors did so is silent about watch.json rather than saying
    there was none, and restoring "none" over a real handoff would destroy it.
    """

    _restore(workspace, remote_stage, "patch.json", recorded.patch)
    if recorded.watch_snapshotted:
        _restore(workspace, remote_stage, "watch.json", recorded.watch)


def _restore(
    workspace: Path,
    remote_stage: RemoteRunStage | None,
    target: str,
    content: str | None,
) -> None:
    if content is None:
        if remote_stage is not None:
            remote_stage.remove_workspace_file(target)
        else:
            (workspace / target).unlink(missing_ok=True)
        return
    if remote_stage is not None:
        remote_stage.write_workspace_text(target, content)
    else:
        (workspace / target).write_text(content, encoding="utf-8")


def absorb_recorded_events(
    outcome: _ProviderOutcome,
    verdict: RecordedVerdict,
) -> list[str]:
    """Read the decoded pass into one outcome, as the live loop would.

    The same five event kinds, meaning the same five things. A recorded turn is
    not handed a conclusion; it reaches one by reading its own events, which is
    why a failure recorded on a host stays a failure here.
    """

    frames = []
    for event in verdict.events:
        if event.session_id:
            outcome.session_id = event.session_id
        if event.event == "session":
            frames.append(_sse(event))
            continue
        if event.event == "answer":
            outcome.answers.append(event.text)
            if event.usage is not None:
                frames.append(_sse(AgentEvent(event="raw", usage=event.usage)))
            continue
        if event.event == "message":
            if event.text.strip() and len(outcome.trace_messages) < 16:
                outcome.trace_messages.append(event.text.strip()[:16_000])
            continue
        if event.event == "error":
            outcome.failed = True
            frames.append(_sse(event))
            continue
        if event.usage is not None:
            frames.append(_sse(AgentEvent(event="raw", usage=event.usage)))
    outcome.completed = verdict.complete and not outcome.failed
    return frames


def attach_retained_stage(
    execution: AgentTaskExecution,
    *,
    owner: str,
    stage_host: str,
    stage_root: str,
    workspace: str,
) -> tuple[Path | None, RemoteRunStage | None, Path]:
    """Reopen the exact stage one launch named, or refuse to open any.

    Recovery attaches; it never opens, sweeps, or prepares. The snapshot names
    the layout that launch used, so a stage that has since moved, become a
    symlink, or stopped being a directory fails here rather than letting a
    finalizer write somewhere the turn never ran.
    """

    if (execution.stage_host or "", execution.stage_root) != (stage_host, stage_root):
        raise ValueError(f"The retained {owner} finalization context belongs to another stage.")
    if stage_host:
        remote_stage = RemoteRunStage(stage_host).attach(stage_root)
        local_stage = None
        allowed_workspaces = {str(remote_stage.workspace)}
    else:
        local_stage = Path(stage_root)
        if not local_stage.is_absolute() or local_stage.is_symlink() or not local_stage.is_dir():
            raise ValueError(f"The retained local {owner} stage is unavailable or unsafe.")
        remote_stage = None
        # Older reusable conversations used the stage itself as the provider
        # cwd. The launch snapshot names which layout this exact turn used.
        allowed_workspaces = {str(local_stage), str(local_stage / "workspace")}
    if workspace not in allowed_workspaces:
        raise ValueError(f"The retained {owner} workspace changed after launch.")
    resolved = Path(workspace)
    if (
        local_stage is not None
        and resolved != local_stage
        and (not resolved.is_dir() or resolved.is_symlink())
    ):
        raise ValueError(f"The retained {owner} workspace is unavailable or unsafe.")
    return local_stage, remote_stage, resolved


def retained_artifact_directory(
    workspace: Path,
    artifact_scope_id: str,
    artifact_directory: str,
    *,
    owner: str,
) -> Path:
    """Confirm the retained artifact boundary is the one this workspace implies."""

    expected = str(workspace / "turns" / artifact_scope_id / "artifacts")
    if artifact_directory != expected:
        raise ValueError(f"The retained {owner} artifact boundary is invalid.")
    return Path(artifact_directory)


__all__ = [
    "absorb_recorded_events",
    "attach_retained_stage",
    "provider_turn_request",
    "retained_artifact_directory",
    "write_recorded_patch",
]
