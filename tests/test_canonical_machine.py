from __future__ import annotations

import pytest

from rcp.agents.launcher import ProviderReadiness
from rcp.config import AgentSurfaceConfig, Manifest
from rcp.history import HistoryManager
from rcp.paper import PaperService
from rcp.providers import ModelChoice
from rcp.service import CoachRequest, ProjectService
from rcp.setup import ProjectSetupRequest
from rcp.storage import AppStore
from tests.helpers import unchecked_write_authority


def test_manifest_rejects_graph_agent_off_canonical_machine(manifest) -> None:
    data = manifest.model_dump(mode="python")
    data["machines"].append({"alias": "remote", "host": "gpu.example"})
    data["agent"]["refresh"] = AgentSurfaceConfig(
        provider="codex",
        reasoning="medium",
        run_on="remote",
    ).model_dump(mode="python")

    with pytest.raises(ValueError, match="canonical state machine"):
        Manifest.model_validate(data)


def test_setup_rejects_graph_profile_off_canonical_machine(tmp_path) -> None:
    local = tmp_path / "local"
    local.mkdir()

    with pytest.raises(ValueError, match="refresh must run beside canonical state"):
        ProjectSetupRequest.model_validate(
            {
                "name": "mixed",
                "repositories": [
                    {
                        "alias": "local",
                        "location": "local",
                        "path": str(local),
                    },
                    {
                        "alias": "remote",
                        "location": "ssh",
                        "host": "gpu.example",
                        "path": "/srv/project",
                    },
                ],
                "state_repository": "local",
                "agents": {
                    "seed": {"location": "local"},
                    "refresh": {"location": "ssh", "host": "gpu.example"},
                    "node_chat": {"location": "local"},
                    "project_chat": {"location": "local"},
                    "paper_coach": {"location": "ssh", "host": "gpu.example"},
                },
            }
        )


@pytest.mark.parametrize("surface", ["refresh", "paper_coach"])
def test_runtime_override_cannot_move_an_agent_off_the_base(manifest, tmp_path, surface) -> None:
    manifest.machines.append(type(manifest.machines[0])(alias="remote", host="gpu.example"))
    history = HistoryManager(manifest, write_authority=unchecked_write_authority())
    store = AppStore(tmp_path / "rcp.sqlite3")
    paper = PaperService(manifest, store, history.workspace, project_id="project")
    service = ProjectService(manifest, history, paper, data_dir=tmp_path / "data")

    with pytest.raises(ValueError, match="canonical state machine"):
        service.resolve_agent_profile(surface, run_on="remote")


def test_provider_override_does_not_inherit_previous_provider_model(manifest, tmp_path) -> None:
    stored = manifest.agent_profile("project_chat")
    manifest.agent.project_chat = stored.model_copy(
        update={"provider": "codex", "model": "gpt-5.6-luna"}
    )
    history = HistoryManager(manifest, write_authority=unchecked_write_authority())
    store = AppStore(tmp_path / "rcp.sqlite3")
    paper = PaperService(manifest, store, history.workspace, project_id="project")
    service = ProjectService(manifest, history, paper, data_dir=tmp_path / "data")

    provider_default = service.resolve_agent_profile(
        "project_chat",
        provider="claude",
        model=None,
    )
    explicit = service.resolve_agent_profile(
        "project_chat",
        provider="claude",
        model="claude-opus-4-1",
    )

    # No readiness probe has run, so the catalog is unknown and the model stays empty.
    assert provider_default.provider == "claude"
    assert provider_default.model == ""
    assert explicit.provider == "claude"
    assert explicit.model == "claude-opus-4-1"


def test_empty_model_resolves_to_the_first_catalogued_model(manifest, tmp_path) -> None:
    history = HistoryManager(manifest, write_authority=unchecked_write_authority())
    store = AppStore(tmp_path / "rcp.sqlite3")
    paper = PaperService(manifest, store, history.workspace, project_id="project")
    service = ProjectService(manifest, history, paper, data_dir=tmp_path / "data")
    machine = manifest.machine_map[manifest.agent_profile("project_chat").run_on]
    probed: list[tuple[str, str, str | None]] = []

    def cached_readiness(provider: str, *, host: str = "", binary: str | None = None):
        probed.append((provider, host, binary))
        if provider == "opencode":
            return ProviderReadiness(
                provider="opencode",
                installed=True,
                authenticated=True,
                models=[ModelChoice(id="opencode/big-pickle", label="Big Pickle", reasoning=[])],
            )
        if provider != "codex":
            return None
        return ProviderReadiness(
            provider="codex",
            installed=True,
            authenticated=True,
            models=[
                ModelChoice(
                    id="gpt-5.6-sol",
                    label="GPT-5.6-Sol",
                    reasoning=["low", "high"],
                    # A catalog default the head itself does not list must not be used.
                    default_reasoning="medium",
                ),
                ModelChoice(id="gpt-5.5", label="GPT-5.5", reasoning=["low", "medium", "high"]),
            ],
        )

    service.launcher.cached_readiness = cached_readiness  # type: ignore[method-assign]

    resolved = service.resolve_agent_profile("project_chat", provider="codex", model="")
    kept_effort = service.resolve_agent_profile(
        "project_chat", provider="codex", model="", reasoning="low"
    )
    explicit = service.resolve_agent_profile(
        "project_chat", provider="codex", model="gpt-5.5", reasoning="medium"
    )
    unknown_catalog = service.resolve_agent_profile("project_chat", provider="claude", model="")
    no_efforts = service.resolve_agent_profile(
        "project_chat", provider="opencode", model="", reasoning="medium"
    )

    assert resolved.model == "gpt-5.6-sol"
    # The profile's `medium` was chosen with no model; the head rejects it and its
    # advertised default is not in its own list, so the first accepted effort is used.
    assert resolved.reasoning == "low"
    assert kept_effort.model == "gpt-5.6-sol"
    assert kept_effort.reasoning == "low"
    assert explicit.model == "gpt-5.5"
    assert explicit.reasoning == "medium"
    assert unknown_catalog.model == ""
    assert (no_efforts.model, no_efforts.reasoning) == ("opencode/big-pickle", "")

    # Both projections export the manifest model unchanged beside the model that
    # runs, so an unnamed profile is never pinned by a display and a readiness
    # refresh re-exports the current head.
    effective = ProjectService.effective_profiles(manifest, service.launcher)
    assert effective["project_chat"]["model"] == ""
    assert effective["project_chat"]["effective_model"] == "gpt-5.6-sol"
    assert effective["project_chat"]["reasoning"] == "medium"
    assert effective["seed"]["effective_model"] == "gpt-5.6-sol"
    assert set(effective) == {
        "seed",
        "refresh",
        "node_chat",
        "project_chat",
        "paper_coach",
        "orchestrator",
    }
    # Only the already-cached probe for that machine's exact executable is read.
    assert probed[0] == ("codex", machine.host, None)


@pytest.mark.asyncio
async def test_standalone_coach_retry_ignores_old_non_base_machine(manifest, tmp_path) -> None:
    from typing import cast

    from rcp.agents import AgentEvent, AgentLauncher
    from rcp.runs.tasks.coach import stream_coach

    from .helpers import create_named_app
    from .paper_helpers import wire_saved_paper
    from .test_coach_suggestions import execution_for

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    wire_saved_paper(service)
    service.manifest.machines.append(
        type(manifest.machines[0])(alias="old-worker", host="old.example")
    )
    execution = execution_for(service)
    execution.continuation = "retry"
    launched = []

    class Launcher:
        async def stream(self, _provider, _prompt, **kwargs):
            launched.append(kwargs)
            assert kwargs["session_id"] is None
            assert kwargs.get("host", "") == ""
            yield AgentEvent(event="session", session_id="new-base-session")
            yield AgentEvent(event="done")

    # No native session or stage exists for the old failed request.
    events = [
        AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
        async for frame in stream_coach(
            service,
            cast(AgentLauncher, Launcher()),
            service.paper,
            CoachRequest(message="Retry coaching", run_on="old-worker"),
            tmp_path / "data",
            execution,
        )
    ]

    assert not [event for event in events if event.event == "error"]
    assert len(launched) == 1
    assert service.paper.sessions()[0].execution_machine == "laptop"


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", ["fresh", "resume", "retry", "refresh"])
async def test_old_coach_session_starts_fresh_on_base(manifest, tmp_path, continuation) -> None:
    from datetime import UTC, datetime
    from typing import cast

    from rcp.agents import AgentEvent, AgentLauncher
    from rcp.paper import WritingSession
    from rcp.runs.tasks.coach import stream_coach
    from rcp.storage import AgentTaskRecord

    from .helpers import create_named_app
    from .paper_helpers import saved, wire_saved_paper
    from .test_coach_suggestions import execution_for

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    live = wire_saved_paper(service)
    now = datetime.now(UTC)
    service.paper.record_session(
        WritingSession(
            provider="codex",
            native_session_id="old-native",
            execution_machine="old-worker",
            project_id=service.paper.project_id,
            model="provider-default",
            reasoning="medium",
            created_at=now,
            last_resumed_at=now,
            introduction_hash_examined="",
            graph_revision_examined=0,
            research_md_hash_examined="",
        )
    )
    execution = execution_for(service)
    if continuation in {"resume", "retry"}:
        execution.continuation = continuation
        execution.stage_host = "old.example"
        execution.stage_root = "/old/checkpoint"
    request = CoachRequest(
        message="Continue coaching", session_id="old-native", run_on="old-worker"
    )
    if continuation == "refresh":
        store = service.paper.store
        store.create_agent_task(
            AgentTaskRecord(
                operation_id="old-task",
                project_id=service.paper.project_id,
                kind="paper_coach",
                status="succeeded",
                request={"provider": "codex", "run_on": "old-worker", "mode": "review"},
                native_session_id="old-native",
                created_at=store.now(),
                updated_at=store.now(),
                status_message="done",
            )
        )
        output = tmp_path / "old-changes"
        (output / "files").mkdir(parents=True)
        (output / "files/paper.md").write_text("Suggested revision\n")
        group = service.paper_changes.collect(
            service.paper.project_id,
            "old-task",
            saved(live, service.paper.project_id),
            output,
        )[0]
        request = request.model_copy(
            update={"refresh_source_task_id": "old-task", "refresh_group_ids": [group.group_id]}
        )
    launched = []

    class Launcher:
        async def stream(self, _provider, _prompt, **kwargs):
            launched.append(kwargs)
            assert kwargs["session_id"] is None
            assert kwargs.get("host", "") == ""
            assert str(kwargs["cwd"]) != "/old/checkpoint"
            yield AgentEvent(event="session", session_id="new-native")
            yield AgentEvent(event="done")

    events = [
        AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
        async for frame in stream_coach(
            service,
            cast(AgentLauncher, Launcher()),
            service.paper,
            request,
            tmp_path / "data",
            execution,
        )
    ]
    assert not [event for event in events if event.event == "error"]
    assert len(launched) == 1
    sessions = {session.native_session_id: session for session in service.paper.sessions()}
    assert sessions["new-native"].execution_machine == "laptop"
    assert sessions["old-native"].execution_machine == "old-worker"
