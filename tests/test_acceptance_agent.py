from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import rcp.__main__ as main_module
from rcp.agents.acceptance import (
    ACCEPTANCE_GENERIC_WATCHER_MARKER,
    AcceptanceAgentLauncher,
)
from rcp.agents.schema import parse_agent_patch_json
from rcp.api import create_app


async def _events(launcher: AcceptanceAgentLauncher, prompt: str, cwd: Path, **kwargs):
    return [
        event
        async for event in launcher.stream(
            "codex",
            prompt,
            cwd=cwd,
            capability="work_auto",
            **kwargs,
        )
    ]


def _prompt(tmp_path: Path, contract: str) -> str:
    path = tmp_path / f"contract-{len(list(tmp_path.glob('contract-*')))}.md"
    path.write_text(contract, encoding="utf-8")
    return f"Open and follow the immutable RCP task contract at:\n{path}\nRead it first."


def _experiment_contract(graph_path: Path) -> str:
    return f"""# RCP Experiment-loop task contract

Required current inputs:
- Current graph, including the Experiment's attempts: `{graph_path}`
- Focused Experiment id: `exp/acceptance-control`
"""


def test_acceptance_app_mode_is_explicit_and_visible(tmp_path) -> None:
    provider_app = create_app(data_dir=tmp_path / "provider-data")
    acceptance_app = create_app(data_dir=tmp_path / "acceptance-data", acceptance_agent=True)

    assert provider_app.state.agent_mode == "provider"
    assert acceptance_app.state.agent_mode == "acceptance"
    assert isinstance(acceptance_app.state.launcher, AcceptanceAgentLauncher)
    with TestClient(provider_app) as client:
        assert client.get("/api/health").json()["agent_mode"] == "provider"
    with TestClient(acceptance_app) as client:
        assert client.get("/api/health").json()["agent_mode"] == "acceptance"


def test_acceptance_agent_cli_flag_is_explicit_and_survives_reload(monkeypatch) -> None:
    parsed = main_module.build_parser().parse_args(["serve", "--acceptance-agent"])
    captured: dict[str, object] = {}
    expected = object()

    def fake_create_app(project, *, instance_metadata, acceptance_agent):
        captured.update(
            project=project,
            instance_metadata=instance_metadata,
            acceptance_agent=acceptance_agent,
        )
        return expected

    monkeypatch.setattr(main_module, "create_app", fake_create_app)
    monkeypatch.delenv(main_module.RELOAD_PROJECT_ENV, raising=False)
    monkeypatch.delenv(main_module.RELOAD_METADATA_ENV, raising=False)
    monkeypatch.setenv(main_module.RELOAD_ACCEPTANCE_AGENT_ENV, "1")

    assert parsed.acceptance_agent is True
    assert main_module.reload_app() is expected
    assert captured == {
        "project": None,
        "instance_metadata": None,
        "acceptance_agent": True,
    }


def test_acceptance_agent_reuse_refuses_a_provider_mode_owner(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    metadata = main_module.ServerMetadata.create(
        tmp_path,
        host="127.0.0.1",
        port=8421,
        owner_kind="cli",
    )
    monkeypatch.setattr(
        main_module,
        "_probe_owner",
        lambda _data_dir: (metadata, {"agent_mode": "provider"}),
    )

    @contextmanager
    def held_lock(_data_dir):
        raise main_module.InstanceLockHeld("held")
        yield

    monkeypatch.setattr(main_module, "instance_lock", held_lock)

    with pytest.raises(SystemExit) as stopped:
        main_module._launch_automatically(
            main_module.build_parser().parse_args(
                ["serve", "--acceptance-agent", "--reuse-existing"]
            ),
            tmp_path,
        )
    assert stopped.value.code == main_module.EXIT_REFUSED_UNAVAILABLE
    assert "requested 'acceptance'" in capsys.readouterr().err


def test_acceptance_agent_reuse_refuses_an_owner_without_an_explicit_mode(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    metadata = main_module.ServerMetadata.create(
        tmp_path,
        host="127.0.0.1",
        port=8421,
        owner_kind="cli",
    )
    monkeypatch.setattr(main_module, "_probe_owner", lambda _data_dir: (metadata, {}))

    @contextmanager
    def held_lock(_data_dir):
        raise main_module.InstanceLockHeld("held")
        yield

    monkeypatch.setattr(main_module, "instance_lock", held_lock)

    with pytest.raises(SystemExit) as stopped:
        main_module._launch_automatically(
            main_module.build_parser().parse_args(["serve", "--reuse-existing"]),
            tmp_path,
        )
    assert stopped.value.code == main_module.EXIT_REFUSED_UNAVAILABLE
    assert "does not report a recognized agent mode" in capsys.readouterr().err


def test_acceptance_launcher_refuses_remote_execution(tmp_path) -> None:
    launcher = AcceptanceAgentLauncher()

    readiness = launcher.readiness("codex", host="fixture.invalid")
    events = asyncio.run(
        _events(
            launcher,
            "not used",
            tmp_path,
            host="fixture.invalid",
        )
    )

    assert readiness.installed is False
    assert readiness.authenticated is False
    assert [event.event for event in events] == ["error"]
    assert not (tmp_path / "watch.json").exists()
    assert launcher.launch_records[0].action == "remote_rejected"


def test_acceptance_experiment_corrects_watchers_then_completes_with_authority_item(
    tmp_path,
) -> None:
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(
        json.dumps(
            {
                "edges": {
                    "edge/acceptance-tests": {
                        "source": "exp/acceptance-control",
                        "target": "hyp/acceptance-sequence",
                        "relation": "tests",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    launcher = AcceptanceAgentLauncher()

    initial = asyncio.run(
        _events(launcher, _prompt(tmp_path, _experiment_contract(graph_path)), tmp_path)
    )
    assert [event.event for event in initial] == ["session", "answer", "provider_exit", "done"]
    assert json.loads((tmp_path / "watch.json").read_text(encoding="utf-8")) == {
        "invalid": "correction required"
    }
    jobs = tmp_path / "acceptance-agent-jobs"
    assert sorted(path.name for path in jobs.glob("*.status")) == [
        "job-one.status",
        "job-two.status",
    ]

    correction = asyncio.run(
        _events(
            launcher,
            _prompt(tmp_path, "# RCP Experiment-loop watcher correction"),
            tmp_path,
            session_id=initial[0].session_id,
        )
    )
    specs = json.loads((tmp_path / "watch.json").read_text(encoding="utf-8"))
    assert [event.event for event in correction] == [
        "session",
        "answer",
        "provider_exit",
        "done",
    ]
    assert len(specs) == 2
    assert launcher.launch_records[-1].action == "watch_correction"
    assert launcher.launch_records[-1].watcher_count == 2
    assert len(list(jobs.glob("*.status"))) == 2

    for name in ("job-one", "job-two"):
        (jobs / f"{name}.done").write_text("done\n", encoding="utf-8")
    wake = asyncio.run(
        _events(launcher, _prompt(tmp_path, _experiment_contract(graph_path)), tmp_path)
    )

    assert [event.event for event in wake] == ["session", "answer", "provider_exit", "done"]
    assert json.loads((tmp_path / "watch.json").read_text(encoding="utf-8")) == []
    patch = parse_agent_patch_json((tmp_path / "patch.json").read_text(encoding="utf-8"))
    payload = patch.model_dump(mode="json")
    assert payload["ops"][0]["nodes"] == [
        {"id": "exp/acceptance-control", "changes": {"status": "completed"}, "cause": None}
    ]
    assert payload["ops"][2]["edges"][1]["id"] == "edge/acceptance-supports"
    proposal_update = payload["ops"][3]["proposals"][0]["ops"][0]["nodes"][0]
    assert proposal_update["id"] == "hyp/acceptance-sequence"
    assert proposal_update["cause"]["ref_id"] == "edge/acceptance-supports"


def test_acceptance_state_survives_a_fresh_launcher_instance(tmp_path) -> None:
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(
        json.dumps(
            {
                "edges": {
                    "edge/acceptance-tests": {
                        "source": "exp/acceptance-control",
                        "target": "hyp/acceptance-sequence",
                        "relation": "tests",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    asyncio.run(
        _events(
            AcceptanceAgentLauncher(),
            _prompt(tmp_path, _experiment_contract(graph_path)),
            tmp_path,
        )
    )

    fresh = AcceptanceAgentLauncher()
    asyncio.run(
        _events(
            fresh,
            _prompt(tmp_path, "# RCP Experiment-loop watcher correction"),
            tmp_path,
            session_id="persisted-native-session",
        )
    )

    assert fresh.launch_records == (
        fresh.launch_records[0].__class__(
            scenario="experiment_loop",
            action="watch_correction",
            cwd=str(tmp_path.resolve()),
            session_id="persisted-native-session",
            watcher_count=2,
        ),
    )


def test_acceptance_generic_marker_arms_two_watchers_without_a_patch(tmp_path) -> None:
    launcher = AcceptanceAgentLauncher()

    asyncio.run(
        _events(
            launcher,
            _prompt(
                tmp_path,
                f"# RCP ordinary Work contract\n\n{ACCEPTANCE_GENERIC_WATCHER_MARKER}",
            ),
            tmp_path,
        )
    )
    asyncio.run(
        _events(
            launcher,
            _prompt(tmp_path, "# RCP watch correction contract"),
            tmp_path,
            session_id=launcher.launch_records[0].session_id,
        )
    )

    specs = json.loads((tmp_path / "watch.json").read_text(encoding="utf-8"))
    assert len(specs) == 2
    assert [record.action for record in launcher.launch_records] == [
        "initial",
        "watch_correction",
    ]
    assert not (tmp_path / "patch.json").exists()
