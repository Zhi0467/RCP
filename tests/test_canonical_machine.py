from __future__ import annotations

import pytest

from rcp.agents.launcher import ProviderReadiness
from rcp.config import AgentSurfaceConfig, Manifest
from rcp.history import HistoryManager
from rcp.paper import PaperService
from rcp.providers import ModelChoice
from rcp.service import ProjectService
from rcp.setup import ProjectSetupRequest
from rcp.storage import AppStore


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


def test_runtime_override_cannot_move_graph_agent(manifest, tmp_path) -> None:
    manifest.machines.append(type(manifest.machines[0])(alias="remote", host="gpu.example"))
    history = HistoryManager(manifest)
    store = AppStore(tmp_path / "rcp.sqlite3")
    paper = PaperService(manifest, store, history.workspace, project_id="project")
    service = ProjectService(manifest, history, paper, data_dir=tmp_path / "data")

    with pytest.raises(ValueError, match="canonical state machine"):
        service.resolve_agent_profile("refresh", run_on="remote")

    coach = service.resolve_agent_profile("paper_coach", run_on="remote")
    assert coach.run_on == "remote"


def test_provider_override_does_not_inherit_previous_provider_model(manifest, tmp_path) -> None:
    stored = manifest.agent_profile("project_chat")
    manifest.agent.project_chat = stored.model_copy(
        update={"provider": "codex", "model": "gpt-5.6-luna"}
    )
    history = HistoryManager(manifest)
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
    history = HistoryManager(manifest)
    store = AppStore(tmp_path / "rcp.sqlite3")
    paper = PaperService(manifest, store, history.workspace, project_id="project")
    service = ProjectService(manifest, history, paper, data_dir=tmp_path / "data")
    machine = manifest.machine_map[manifest.agent_profile("project_chat").run_on]
    probed: list[tuple[str, str, str | None]] = []

    def cached_readiness(provider: str, *, host: str = "", binary: str | None = None):
        probed.append((provider, host, binary))
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

    assert resolved.model == "gpt-5.6-sol"
    # The profile's `medium` was chosen with no model; the head rejects it and its
    # advertised default is not in its own list, so the first accepted effort is used.
    assert resolved.reasoning == "low"
    assert kept_effort.model == "gpt-5.6-sol"
    assert kept_effort.reasoning == "low"
    assert explicit.model == "gpt-5.5"
    assert explicit.reasoning == "medium"
    assert unknown_catalog.model == ""

    # The same fill feeds both projections, so a readiness refresh re-exports it.
    effective = ProjectService.effective_profiles(manifest, service.launcher)
    assert effective["project_chat"]["model"] == "gpt-5.6-sol"
    assert effective["project_chat"]["reasoning"] == "low"
    assert set(effective) == {
        "seed",
        "refresh",
        "node_chat",
        "project_chat",
        "paper_coach",
        "orchestrator",
    }
    # Only the already-cached probe for that machine's exact executable is read.
    assert probed[0] == ("codex", machine.host, machine.provider_paths.get("codex"))
