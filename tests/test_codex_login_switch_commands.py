from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from rcp.agents import AgentLauncher
from rcp.agents.provider_environment import ProviderCredentialStore
from rcp.providers.base import ProviderTurnRequest
from rcp.providers.codex.auth import CodexAuthentication
from rcp.providers.codex.profile import CodexProfile


@pytest.mark.parametrize("runtime", ["codex.exec-json.v1", "codex.app-server-stdio.v1"])
@pytest.mark.parametrize("host", ["", "worker"])
@pytest.mark.parametrize("fork", [False, True])
def test_login_switch_preserves_session_command_and_execution_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: str, host: str, fork: bool
) -> None:
    machine = tmp_path / "machine-codex"
    monkeypatch.setenv("CODEX_HOME", str(machine))
    credentials = ProviderCredentialStore(tmp_path / "providers")
    login_ids = [str(uuid4()), str(uuid4())]
    prepared = []

    def prepare_remote(command, **_kwargs):
        prepared.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("rcp.providers.codex.auth.subprocess.run", prepare_remote)
    environments = [
        CodexAuthentication().process_environment(credentials, host, login_id)
        for login_id in login_ids
    ]
    session_id = str(uuid4())
    cwd = Path("/workspace/project") if host else tmp_path
    turn = (
        CodexProfile()
        .runtime(runtime)
        .turn(
            ProviderTurnRequest(
                prompt="Continue",
                binary="codex",
                cwd=cwd,
                model=None,
                reasoning=None,
                session_id=None if fork else session_id,
                fork_session_id=session_id if fork else None,
                read_dirs=[],
                write_dirs=[],
                write_scope=None,
                capability="discuss",
                provider_version="0.165.0",
            )
        )
    )
    if runtime == "codex.exec-json.v1":
        assert turn.command[:3] == ["codex", "exec", "fork" if fork else "resume"]
        assert turn.command[-2:] == [session_id, "-"]
    else:
        assert turn.command[:2] == ["codex", "app-server"]
        turn.receive_line(json.dumps({"id": 1, "result": {}}))
        step = turn.receive_line(json.dumps({"id": 2, "result": {"config": {}}}))
        message = json.loads(step.outgoing[0])
        assert message["method"] == ("thread/fork" if fork else "thread/resume")
        assert message["params"]["threadId"] == session_id

    if host:
        assert len(prepared) == 2
        for login_id, preparation, environment in zip(
            login_ids, prepared, environments, strict=True
        ):
            home = f"~/.rcp/provider-homes/codex/{login_id}"
            preparation_shell = shlex.split(preparation[-1])
            assert shlex.split(preparation_shell[-1])[-1] == home
            shell = shlex.split(
                AgentLauncher._remote_login_command(
                    turn.command, cwd=cwd, prefix=environment.remote_prefix
                )
            )
            assert shell[:2] == ["bash", "-lic"]
            assert f'export CODEX_HOME="$HOME"/.rcp/provider-homes/codex/{login_id};' in shell[2]
            assert shell[2].endswith(shlex.join(turn.command))
            assert environment.local_env is None
    else:
        homes = []
        for login_id, environment in zip(login_ids, environments, strict=True):
            assert environment.local_env is not None
            home = Path(environment.local_env["CODEX_HOME"])
            assert home == credentials.account_dir("codex", "", login_id) / "codex-home"
            assert (home / "sessions").resolve() == machine / "sessions"
            homes.append(home)
        rollout = Path("sessions/2026/10/30") / f"rollout-example-{session_id}.jsonl"
        (homes[0] / rollout).parent.mkdir(parents=True)
        (homes[0] / rollout).write_text('{"type":"session_meta"}\n')
        assert (homes[1] / rollout).read_bytes() == (homes[0] / rollout).read_bytes()
