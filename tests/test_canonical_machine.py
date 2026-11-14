from __future__ import annotations

import pytest

from rcp.agents.launcher import ProviderReadiness
from rcp.history import HistoryManager
from rcp.machine_cards import LoginKey
from rcp.paper import PaperService
from rcp.providers import ModelChoice
from rcp.service import ProjectService
from rcp.setup import ProjectSetupRequest
from rcp.storage import AppStore
from tests.helpers import unchecked_write_authority


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
    manifest.machines.append("c" * 32)
    history = HistoryManager(manifest, write_authority=unchecked_write_authority())
    store = AppStore(tmp_path / "rcp.sqlite3")
    paper = PaperService(manifest, store, history.workspace, project_id="project")
    service = ProjectService(manifest, history, paper, data_dir=tmp_path / "data")

    with pytest.raises(ValueError):
        service.resolve_agent_profile(surface, run_on="c" * 32)


def test_provider_override_does_not_inherit_previous_provider_model(manifest, tmp_path) -> None:
    """No readiness probe has run, so the catalog is unknown and the model stays empty."""
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

    assert provider_default.provider == "claude"
    assert provider_default.model == ""
    assert explicit.provider == "claude"
    assert explicit.model == "claude-opus-4-1"


def test_empty_model_resolves_to_the_first_catalogued_model(manifest, tmp_path) -> None:
    """The profile's `medium` was chosen with no model; the head rejects it and its advertised
    default is unlisted, so the first accepted effort is used. Both projections export the manifest
    model unchanged so a display never pins an unnamed profile, and only the cached probe for the
    machine's exact executable is read.
    """
    history = HistoryManager(manifest, write_authority=unchecked_write_authority())
    store = AppStore(tmp_path / "rcp.sqlite3")
    paper = PaperService(manifest, store, history.workspace, project_id="project")
    service = ProjectService(manifest, history, paper, data_dir=tmp_path / "data")
    probed: list[tuple[str, str, str | None]] = []

    def cached_readiness(login: LoginKey, *, binary: str | None = None):
        """A catalog default the head itself does not list must not be used."""
        probed.append((login.provider, login.machine_id, binary))
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
    assert resolved.reasoning == "low"
    assert kept_effort.model == "gpt-5.6-sol"
    assert kept_effort.reasoning == "low"
    assert explicit.model == "gpt-5.5"
    assert explicit.reasoning == "medium"
    assert unknown_catalog.model == ""
    assert (no_efforts.model, no_efforts.reasoning) == ("opencode/big-pickle", "")

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
    assert probed[0] == ("codex", manifest.base_machine, None)
