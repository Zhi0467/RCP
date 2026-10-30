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
from rcp.storage.models import LoginKey
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

    with pytest.raises(ValueError):
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

    def cached_readiness(login: LoginKey, *, binary: str | None = None):
        probed.append((login.provider, login.host, binary))
        if login.provider == "opencode":
            return ProviderReadiness(
                provider="opencode",
                installed=True,
                authenticated=True,
                models=[ModelChoice(id="opencode/big-pickle", label="Big Pickle", reasoning=[])],
            )
        if login.provider != "codex":
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
async def test_old_coach_checkpoint_starts_fresh_on_base(manifest, tmp_path) -> None:
    from typing import cast

    from rcp.agents import AgentEvent, AgentLauncher
    from rcp.runs.tasks.coach import stream_coach

    from .helpers import create_named_app
    from .paper_helpers import wire_saved_paper
    from .test_coach_suggestions import execution_for

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    wire_saved_paper(service)
    execution = execution_for(service)
    execution.continuation = "resume"
    execution.stage_host = "old.example"
    execution.stage_root = "/old/checkpoint"
    request = CoachRequest(
        message="Continue coaching", session_id="old-native", run_on="old-worker"
    )
    launched = []

    class Launcher:
        async def stream(self, _provider, _prompt, **kwargs):
            launched.append(kwargs)
            assert kwargs["session_id"] is None
            assert kwargs.get("host", "") == manifest.machine_map[manifest.base_machine].host
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
    assert launched
    assert not [event for event in events if event.event == "error"]
    sessions = {session.native_session_id: session for session in service.paper.sessions()}
    assert sessions["new-native"].execution_machine == manifest.base_machine
