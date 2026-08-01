from __future__ import annotations

import subprocess

from fastapi.testclient import TestClient

from rcp.agents import ProviderReadiness
from rcp.api import create_app
from rcp.config import load_manifest
from rcp.setup import ProjectSetupRequest, SetupAgents, render_manifest


def test_setup_agent_profiles_no_longer_carry_a_write_mode() -> None:
    agents = SetupAgents()

    for surface in ("seed", "refresh", "node_chat", "project_chat", "paper_coach"):
        profile = agents.profile(surface)
        assert not hasattr(profile, "write_path")
        assert "write_path" not in profile.model_dump()


def _local_payload(repository_path: str) -> dict[str, object]:
    return {
        "name": "wizard-paper",
        "repositories": [
            {
                "alias": "paper-repo",
                "location": "local",
                "path": repository_path,
                "host": "",
                "default_read": True,
            }
        ],
        "state_repository": "paper-repo",
        "execution": {
            "location": "local",
            "host": "",
        },
        "confirmed": False,
    }


def test_local_wizard_preflights_without_writing_then_creates(tmp_path) -> None:
    repository = tmp_path / "paper"
    repository.mkdir()
    app = create_app(data_dir=tmp_path / "data")
    client = TestClient(app)
    payload = _local_payload(str(repository))

    preview = client.post("/api/project-setup/preflight", json=payload)

    assert preview.status_code == 200
    assert preview.json()["action"] == "create"
    assert preview.json()["can_create"] is True
    assert not (repository / ".research").exists()

    unconfirmed = client.post("/api/project-setup/create", json=payload)
    assert unconfirmed.status_code == 422
    assert not (repository / ".research").exists()

    payload["confirmed"] = True
    created = client.post("/api/project-setup/create", json=payload)

    assert created.status_code == 200
    assert created.json()["name"] == "wizard-paper"
    assert created.json()["revision"] == 0
    assert created.json()["reachable"] is True
    assert (repository / ".research" / "manifest.toml").is_file()
    assert client.get("/api/projects").json()[0]["id"] == created.json()["id"]


def test_setup_records_discovered_provider_paths_in_new_manifest(tmp_path) -> None:
    repository = tmp_path / "paper"
    repository.mkdir()
    app = create_app(data_dir=tmp_path / "data")

    class DiscoveringLauncher:
        @staticmethod
        def readiness(provider: str, *, host: str = "") -> ProviderReadiness:
            assert host == ""
            return ProviderReadiness(
                provider=provider,
                installed=True,
                authenticated=True,
                binary_path=f"/opt/rcp-test/{provider}",
                path_state="unconfigured",
            )

    app.state.setup.launcher = DiscoveringLauncher()
    client = TestClient(app)
    payload = _local_payload(str(repository))
    payload["confirmed"] = True

    created = client.post("/api/project-setup/create", json=payload)

    assert created.status_code == 200
    manifest = load_manifest(repository / ".research" / "manifest.toml")
    assert manifest.machine_map["laptop"].provider_paths == {
        "codex": "/opt/rcp-test/codex",
        "claude": "/opt/rcp-test/claude",
    }


def test_existing_local_manifest_is_connected_without_overwrite(tmp_path) -> None:
    repository = tmp_path / "paper"
    repository.mkdir()
    app = create_app(data_dir=tmp_path / "data")
    client = TestClient(app)
    payload = _local_payload(str(repository))
    payload["confirmed"] = True
    assert client.post("/api/project-setup/create", json=payload).status_code == 200
    manifest = repository / ".research" / "manifest.toml"
    original = manifest.read_text(encoding="utf-8")

    payload["name"] = "a-name-that-must-not-overwrite"
    payload["confirmed"] = False
    preview = client.post("/api/project-setup/preflight", json=payload)

    assert preview.status_code == 200
    assert preview.json()["action"] == "connect"
    assert preview.json()["existing_project_name"] == "wizard-paper"
    assert manifest.read_text(encoding="utf-8") == original


def test_wizard_rejects_blank_name_and_invalid_state_path(tmp_path) -> None:
    repository = tmp_path / "paper"
    repository.mkdir()
    app = create_app(data_dir=tmp_path / "data")
    client = TestClient(app)
    blank = _local_payload(str(repository))
    blank["name"] = "   "

    assert client.post("/api/project-setup/preflight", json=blank).status_code == 422

    (repository / ".research").write_text("not a directory", encoding="utf-8")
    preview = client.post("/api/project-setup/preflight", json=_local_payload(str(repository)))

    assert preview.status_code == 200
    assert preview.json()["can_create"] is False
    assert "not a directory" in preview.json()["checks"][1]["detail"]


def test_remote_preflight_checks_ssh_without_writing(monkeypatch, tmp_path) -> None:
    calls: list[list[str]] = []

    def fake_ssh(host: str, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        assert host == "gpu.example"
        calls.append(arguments)
        if arguments[0] == "cat" or arguments[:2] in (["test", "-f"], ["test", "-e"]):
            return subprocess.CompletedProcess([], 1, "", "")
        return subprocess.CompletedProcess([], 0, "", "")

    class ReadyLauncher:
        @staticmethod
        def readiness(provider: str, *, host: str = "") -> ProviderReadiness:
            return ProviderReadiness(
                provider=provider,
                installed=True,
                authenticated=True,
                version=f"{provider}-test",
            )

    monkeypatch.setattr("rcp.setup._ssh", fake_ssh)
    app = create_app(data_dir=tmp_path / "data")
    app.state.setup.launcher = ReadyLauncher()
    request = ProjectSetupRequest.model_validate(
        {
            "name": "remote-paper",
            "repositories": [
                {
                    "alias": "remote-repo",
                    "location": "ssh",
                    "host": "gpu.example",
                    "path": "/srv/paper",
                    "default_read": True,
                }
            ],
            "state_repository": "remote-repo",
            "execution": {
                "location": "ssh",
                "host": "gpu.example",
            },
        }
    )

    preview = app.state.setup.preflight(request)

    assert preview.can_create is True
    assert preview.remote_write is True
    assert preview.canonical_location == "gpu.example:/srv/paper/.research"
    assert 'host = "gpu.example"' in preview.manifest_preview
    assert ["test", "-d", "/srv/paper"] in calls
    assert ["test", "-w", "/srv/paper"] in calls
    assert not any(arguments[0] in {"mkdir", "touch", "rm"} for arguments in calls)


def test_wizard_manifest_records_each_agent_role_and_fixed_permissions(tmp_path) -> None:
    repository = tmp_path / "paper"
    repository.mkdir()
    request = ProjectSetupRequest.model_validate(
        {
            "name": "configured-paper",
            "repositories": [
                {
                    "alias": "paper-repo",
                    "location": "local",
                    "path": str(repository),
                    "default_read": True,
                }
            ],
            "state_repository": "paper-repo",
            "agents": {
                "seed": {
                    "provider": "claude",
                    "model": "claude-seed",
                    "reasoning": "high",
                    "location": "local",
                },
                "refresh": {
                    "provider": "codex",
                    "model": "codex-refresh",
                    "reasoning": "medium",
                    "location": "local",
                },
                "node_chat": {
                    "provider": "claude",
                    "model": "claude-node",
                    "reasoning": "low",
                    "location": "local",
                },
                "project_chat": {
                    "provider": "codex",
                    "model": "codex-project",
                    "reasoning": "xhigh",
                    "location": "local",
                },
                "paper_coach": {
                    "provider": "claude",
                    "model": "claude-coach",
                    "reasoning": "medium",
                    "location": "local",
                },
            },
        }
    )
    research = repository / ".research"
    research.mkdir()
    path = research / "manifest.toml"
    rendered = render_manifest(request)
    path.write_text(rendered, encoding="utf-8")

    manifest = load_manifest(path)

    assert "[execution]" not in rendered
    assert "write_path" not in rendered
    assert manifest.agent_profile("seed").provider == "claude"
    assert manifest.agent_profile("project_chat").reasoning == "xhigh"
    assert manifest.agent_profile("paper_coach").model == "claude-coach"
    assert manifest.agent_profile("paper_coach").permissions.write_graph_patch is False
    assert manifest.agent_profile("refresh").permissions.read_repositories == "run_scope"
