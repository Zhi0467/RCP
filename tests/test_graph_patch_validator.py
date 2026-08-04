from __future__ import annotations

import asyncio
import json
import re
import shlex
import subprocess
from pathlib import Path

import pytest

import rcp.runs.graph as graph_run
from rcp.agents import AgentEvent
from rcp.api import create_app
from rcp.runs.graph import stream_graph_run
from rcp.runs.patch_validator import VALIDATOR_CLIENT_SOURCE
from rcp.service import RunRequest
from tests.helpers import agent_patch_json, seed_patch


@pytest.mark.asyncio
async def test_seed_attempt_stages_and_serves_live_validator_before_final_append(
    manifest, tmp_path: Path, monkeypatch
) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    monkeypatch.setattr(graph_run, "PATCH_SELF_CHECK_TIMEOUT_SECONDS", 2)

    class ValidatingLauncher:
        validator_result: subprocess.CompletedProcess[str] | None = None

        async def stream(self, _provider, prompt, **kwargs):
            workspace = Path(kwargs["cwd"])
            contract_path = Path(prompt.splitlines()[1])
            contract = contract_path.read_text(encoding="utf-8")
            command_match = re.search(
                r"After writing `patch\.json`, run this exact command: `([^`]+)`",
                contract,
            )
            assert command_match is not None
            command = shlex.split(command_match.group(1))
            validator_client = Path(command[1])
            assert validator_client.read_text(encoding="utf-8") == VALIDATOR_CLIENT_SOURCE

            (workspace / "patch.json").write_text(agent_patch_json(seed_patch()), encoding="utf-8")
            self.validator_result = await asyncio.to_thread(
                subprocess.run,
                command,
                capture_output=True,
                text=True,
                check=False,
            )
            assert self.validator_result.returncode == 0
            assert json.loads(self.validator_result.stdout)["status"] == "valid"
            assert service.history.state().revision == 0
            yield AgentEvent(event="session", session_id="seed-validator-session")
            yield AgentEvent(event="done")

    launcher = ValidatingLauncher()
    frames = [
        frame
        async for frame in stream_graph_run(
            service,
            launcher,
            "seed",
            RunRequest(run_truth_scope=["repo-a"]),
            tmp_path / "data",
        )
    ]

    assert launcher.validator_result is not None
    assert service.history.state().revision == 1
    assert any("applied_revision" in frame for frame in frames)
    assert '"event":"done"' in frames[-1]
