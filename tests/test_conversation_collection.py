from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import socket
import subprocess
import threading
import uuid
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from rcp.agents import AgentEvent, ProviderReadiness
from rcp.agents.launcher import AgentProcessControl
from rcp.background import BackgroundAgentTasks
from rcp.providers import profile_for
from rcp.runs.tasks.work import stream_work_run
from rcp.transport import LocalStateWorkspace, RemoteRunStage

from .helpers import (
    agent_patch_json,
    append_fixture_patch,
    create_named_app,
    refresh_patch,
    seed_patch,
    shape_invalid_patch,
    wait_for_task_response,
    wait_until,
)


@pytest.fixture
def local_remote(monkeypatch):
    """Run the actual remote scripts against only disposable local stages."""
    run = subprocess.run
    roots: set[str] = set()
    open_stage = RemoteRunStage.open

    def tracked_open(self, *args, **kwargs):
        result = open_stage(self, *args, **kwargs)
        roots.add(str(self.root))
        return result

    def command(arguments, **kwargs):
        if arguments[0] == "rsync":
            target = arguments[-1]
            assert target.startswith("collection-test:")
            destination = shlex.split(target.split(":", 1)[1])[0].rstrip("/")
            shutil.copytree(arguments[-2], destination)
            return subprocess.CompletedProcess(arguments, 0, "", "")
        assert arguments[0] != "ssh", "Integration tests must never contact an execution host."
        return run(arguments, **kwargs)

    monkeypatch.setattr(subprocess, "run", command)
    monkeypatch.setattr(RemoteRunStage, "sweep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(RemoteRunStage, "open", tracked_open)

    def ssh(_self, args):
        result = command(args, capture_output=True, text=True, check=False)
        # The execution host uses Linux paths; macOS aliases /tmp to /private/tmp.
        return subprocess.CompletedProcess(
            result.args,
            result.returncode,
            result.stdout.replace("/private/tmp/rcp-run.", "/tmp/rcp-run."),
            result.stderr,
        )

    monkeypatch.setattr(RemoteRunStage, "_ssh", ssh)
    monkeypatch.setattr(
        RemoteRunStage,
        "_ssh_bytes",
        lambda _self, args, input_data=None, **_kwargs: command(
            args,
            capture_output=True,
            input=input_data,
            check=False,
        ),
    )
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_: True)
    yield
    for root in roots:
        RemoteRunStage("collection-test").attach(root).close()


class JournaledWorkLauncher:
    """One completed provider pass whose result never reached the live pipe."""

    def __init__(self, patch: str, *, partial_usage: bool = False):
        self.patch = patch
        self.partial_usage = partial_usage
        self.calls = 0
        self.native_session_id = str(uuid.uuid4())
        self.repair_patch: str | None = None

    async def stream(self, _provider, _prompt, **kwargs):
        self.calls += 1
        workspace = Path(kwargs["cwd"])
        if self.calls == 1:
            # The launcher announces its remote pass before the SSH command can
            # run, and its runtime only once that pass is handed the turn.
            yield AgentEvent(event="remote_process_start", text=kwargs["remote_pid_file"])
        yield AgentEvent(event="runtime", text="codex.exec-json.v1")
        yield AgentEvent(event="session", session_id=self.native_session_id)
        if self.calls > 1:
            assert self.repair_patch is not None, "Collection must not launch another provider."
            (workspace / "patch.json").write_text(self.repair_patch)
            yield AgentEvent(event="answer", text="Repaired the graph reflection.")
            yield AgentEvent(event="done")
            return
        pid = kwargs["remote_pid_file"]
        journal = Path(pid + ".turn")
        journal.mkdir(mode=0o700)
        terminal = {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20}}
        events = (
            "\n".join(
                json.dumps(item)
                for item in (
                    {"type": "thread.started", "thread_id": self.native_session_id},
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "The original work is complete."},
                    },
                    terminal,
                )
            )
            + "\n"
        )
        (workspace / "patch.json").write_text(self.patch)
        (journal / "events.jsonl").write_text(events)
        (journal / "patch.json").write_text(self.patch)
        (journal / "outcome.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "pid_file": pid,
                    "provider": "codex",
                    "runtime_id": "codex.exec-json.v1",
                    "protocol_complete": True,
                    "journal_complete": True,
                    "patch_present": True,
                    "events_sha256": hashlib.sha256(events.encode()).hexdigest(),
                    "patch_sha256": hashlib.sha256(self.patch.encode()).hexdigest(),
                }
            )
        )
        if self.partial_usage:
            decoded = profile_for("codex").decode_event(terminal, json.dumps(terminal))
            yield AgentEvent(event="raw", usage=decoded.usage)
        yield AgentEvent(
            event="provider_exit",
            text=json.dumps(
                {
                    "return_code": 255,
                    "remote_process_stopped": False,
                    "event_counts": {},
                }
            ),
        )
        yield AgentEvent(event="error", text="The execution host disconnected.")


def collection_app(manifest, tmp_path, monkeypatch, launcher):
    manifest.path.write_text(
        manifest.path.read_text().replace('host = ""', 'host = "collection-test"')
    )
    monkeypatch.setattr(
        "rcp.projects.state_workspace_for_probe",
        lambda bootstrap, _data_dir: LocalStateWorkspace(
            bootstrap.research_dir, str(bootstrap.research_dir)
        ),
    )
    monkeypatch.setattr(
        "rcp.projects.prepare_state_workspace",
        lambda bootstrap, _data_dir: (
            bootstrap,
            LocalStateWorkspace(bootstrap.research_dir, str(bootstrap.research_dir)),
        ),
    )
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    append_fixture_patch(app.state.service, seed_patch())

    async def stream(_project_id, _kind, request, execution):
        async for frame in stream_work_run(
            app.state.service, launcher, request, tmp_path / "data", execution=execution
        ):
            yield frame

    app.state.background_tasks.stream = stream
    monkeypatch.setattr(
        app.state.background_tasks, "_schedule_transport_retry", lambda *_args, **_kwargs: None
    )
    return app


def fail_original(client, project_id):
    response = client.post(
        f"/api/projects/{project_id}/tasks/project_chat",
        json={
            "provider": "codex",
            "run_on": "laptop",
            "chat_id": str(uuid.uuid4()),
            "message": "Complete the work and reflect its result.",
            "mode": "work",
            "run_truth_scope": ["repo-a"],
        },
    )
    assert response.status_code == 202, response.text
    failed = wait_for_task_response(client, project_id, response.json()["operation_id"])
    assert failed["status"] == "failed", failed
    assert failed["can_collect"] is True, failed.get("error")
    return failed


@pytest.mark.parametrize("partial_usage", [False, True])
@pytest.mark.parametrize("recovery_route", ["collect", "retry"])
def test_collect_work_preserves_answer_applies_once_and_counts_usage_once(
    manifest,
    tmp_path,
    monkeypatch,
    local_remote,
    partial_usage,
    recovery_route,
):
    patch = agent_patch_json(refresh_patch("rq/collected-work").model_copy(update={"kind": "work"}))
    launcher = JournaledWorkLauncher(patch, partial_usage=partial_usage)
    app = collection_app(manifest, tmp_path, monkeypatch, launcher)
    client = TestClient(app)
    project_id = app.state.default_project_id
    original = fail_original(client, project_id)
    before_revision = app.state.service.history.state().revision
    response = client.post(
        f"/api/projects/{project_id}/tasks/{original['operation_id']}/{recovery_route}"
    )
    assert response.status_code == 202, response.text
    collected = wait_for_task_response(client, project_id, response.json()["operation_id"])
    assert collected["status"] == "succeeded", collected
    assert collected["parent_operation_id"] == original["operation_id"]
    assert collected["chat_turn_operation_id"] == original["operation_id"]
    assert collected["authorized_by"] == original["authorized_by"]
    assert collected["graph_target"] == original["graph_target"]
    assert collected["stage_root"] == original["stage_root"]
    assert collected["native_session_id"] == original["native_session_id"]
    assert collected["result"]["messages"] == ["The original work is complete."]
    assert collected["result"]["graph_update"]["status"] == "applied"
    assert launcher.calls == 1
    assert app.state.service.history.state().revision == before_revision + 1
    assert "rq/collected-work" in app.state.service.history.state().nodes
    duplicate = client.post(f"/api/projects/{project_id}/tasks/{original['operation_id']}/collect")
    assert duplicate.status_code == 409
    assert app.state.service.history.state().revision == before_revision + 1
    records = app.state.background_tasks.store.agent_usage(project_id)
    assert sum(item.generated_tokens for item in records if item.counted) == 20


def test_collected_rejected_work_uses_existing_manual_repair(
    manifest,
    tmp_path,
    monkeypatch,
    local_remote,
):
    invalid = agent_patch_json(shape_invalid_patch().model_copy(update={"kind": "work"}))
    launcher = JournaledWorkLauncher(invalid)
    app = collection_app(manifest, tmp_path, monkeypatch, launcher)
    client = TestClient(app)
    project_id = app.state.default_project_id
    original = fail_original(client, project_id)
    response = client.post(f"/api/projects/{project_id}/tasks/{original['operation_id']}/collect")
    assert response.status_code == 202, response.text
    collected = wait_for_task_response(client, project_id, response.json()["operation_id"])
    assert collected["status"] == "succeeded", collected
    assert collected["result"]["messages"] == ["The original work is complete."]
    assert collected["result"]["graph_update"]["status"] == "rejected"
    assert collected["result"]["graph_update"]["repairable"] is True
    assert launcher.calls == 1
    launcher.repair_patch = agent_patch_json(
        refresh_patch("rq/repaired-collected-work").model_copy(update={"kind": "work"})
    )
    repair = client.post(
        f"/api/projects/{project_id}/tasks/{collected['operation_id']}/repair-graph-update"
    )
    assert repair.status_code == 202, repair.text
    repaired = wait_for_task_response(client, project_id, repair.json()["operation_id"])
    assert repaired["status"] == "succeeded", repaired
    assert repaired["result"]["graph_update"]["status"] == "applied"
    assert launcher.calls == 2
    assert "rq/repaired-collected-work" in app.state.service.history.state().nodes


def test_reconnect_automatically_applies_and_publishes_chat_once(
    manifest,
    tmp_path,
    monkeypatch,
    local_remote,
):
    patch = agent_patch_json(
        refresh_patch("rq/automatic-collection").model_copy(update={"kind": "work"})
    )
    launcher = JournaledWorkLauncher(patch)
    app = collection_app(manifest, tmp_path, monkeypatch, launcher)
    client = TestClient(app)
    project_id = app.state.default_project_id
    original = fail_original(client, project_id)
    tasks = app.state.background_tasks
    before_revision = app.state.service.history.state().revision
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_: False)
    pending = client.post(f"/api/projects/{project_id}/tasks/{original['operation_id']}/collect")
    assert pending.status_code == 409
    assert len(tasks.store.agent_tasks(project_id)) == 1
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_: True)
    monkeypatch.setattr("rcp.background.AGENT_TRANSPORT_RETRY_BACKOFF_SECONDS", (0.01, 0.01, 0.01))
    monkeypatch.setattr(
        tasks,
        "_schedule_transport_retry",
        BackgroundAgentTasks._schedule_transport_retry.__get__(tasks),
    )
    # This is the startup/reconnect owner, not a human Collect or Retry request.
    tasks._rearm_owed_transport_retries()
    child = wait_until(
        lambda: next(
            (
                item
                for item in tasks.store.agent_tasks(project_id)
                if item.parent_operation_id == original["operation_id"]
            ),
            None,
        )
    )
    collected = wait_for_task_response(client, project_id, child.operation_id)
    assert collected["status"] == "succeeded", collected
    assert app.state.service.history.state().revision == before_revision + 1
    chat_id = original["request"]["chat_id"]
    for _ in range(3):
        tasks._rearm_owed_transport_retries()
        transcript = client.get(f"/api/projects/{project_id}/chats/{chat_id}")
        assert transcript.status_code == 200, transcript.text
        answers = [
            item["text"] for item in transcript.json()["messages"] if item["role"] == "assistant"
        ]
        assert answers == ["The original work is complete."]
        assert app.state.service.history.state().revision == before_revision + 1
    assert launcher.calls == 1
    assert len(tasks.store.agent_tasks(project_id)) == 2


def test_served_browser_reconnect_catches_up_graph_and_chat_without_collect_click(
    manifest,
    tmp_path,
    monkeypatch,
    local_remote,
    caplog,
):
    web_root = Path(__file__).resolve().parents[1] / "web"
    if shutil.which("node") is None:
        pytest.skip("Served browser qualification requires Node and Playwright Chromium.")
    browser_path = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            'import { chromium } from "playwright"; console.log(chromium.executablePath());',
        ],
        cwd=web_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if browser_path.returncode != 0 or not Path(browser_path.stdout.strip()).is_file():
        pytest.skip(
            "Install Playwright Chromium as documented in README.md for browser qualification."
        )
    patch = agent_patch_json(
        refresh_patch("rq/browser-collected").model_copy(update={"kind": "work"})
    )
    launcher = JournaledWorkLauncher(patch)
    app = collection_app(manifest, tmp_path, monkeypatch, launcher)
    monkeypatch.setattr(
        app.state.services.launcher,
        "readiness",
        lambda provider, **_: ProviderReadiness(
            provider=provider,
            installed=False,
            authenticated=False,
            reason="Disposable test provider.",
        ),
    )
    client = TestClient(app)
    project_id = app.state.default_project_id
    original = fail_original(client, project_id)
    assert client.get(f"/api/projects/{project_id}").status_code == 200
    initial_revision = app.state.service.history.state().revision
    tasks = app.state.background_tasks
    finished = threading.Event()
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_: finished.is_set())
    monkeypatch.setattr("rcp.background.AGENT_TRANSPORT_RETRY_BACKOFF_SECONDS", (0.05, 0.05, 0.05))
    monkeypatch.setattr(
        tasks,
        "_schedule_transport_retry",
        BackgroundAgentTasks._schedule_transport_retry.__get__(tasks),
    )
    ready = tmp_path / "browser-ready"
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    node_source = r"""
import assert from "node:assert/strict";
import { writeFile } from "node:fs/promises";
import { chromium } from "playwright";
const [url, project, chat, ready, revision] = process.argv.slice(2);
const browser = await chromium.launch({headless:true});
try {
  const page = await browser.newPage();
  const errors = [], failures = [], requests = [];
  page.on("pageerror", error => errors.push(String(error)));
  page.on("console", message => { if (message.type() === "error") errors.push({text:message.text(),location:message.location()}); });
  page.on("response", response => { if(response.status() >= 500) failures.push(`${response.status()} ${response.url()}`); });
  page.on("request", request => requests.push(`${request.method()} ${request.url()}`));
  await page.goto(`${url}/#/projects/${project}?view=chats`);
  await page.getByRole("button", {name:"Collect result", exact:true}).waitFor({timeout:20000}).catch(async error => { console.error(await page.locator("body").innerText()); throw error; });
  await writeFile(ready, "ready");
  await page.getByText("The original work is complete.", {exact:true}).waitFor({timeout:20000}).catch(async error => { console.error(JSON.stringify({body:await page.locator("body").innerText(), url:page.url(),errors,failures,requests})); throw error; });
  assert.equal(await page.getByText("The original work is complete.", {exact:true}).count(), 1);
  assert.equal(requests.some(request => request.startsWith("POST ") && /\/(collect|retry)$/.test(request)), false);
  const graph = await page.evaluate(async (project) => (await fetch(`/api/projects/${project}`)).json(), project);
  assert.equal(graph.graph.revision, Number(revision) + 1);
  assert.ok(graph.graph.nodes["rq/browser-collected"]);
  await page.reload();
  await page.waitForURL(`${url}/`);
  await page.evaluate(hash => { window.location.hash = hash; }, `/projects/${project}?view=chats&chat=${chat}`);
  await page.getByText("The original work is complete.", {exact:true}).waitFor({timeout:20000}).catch(async error => { console.error(JSON.stringify({body:await page.locator("body").innerText(), url:page.url(),errors,failures,requests})); throw error; });
  assert.equal(await page.getByText("The original work is complete.", {exact:true}).count(), 1);
  assert.deepEqual(errors, []);
  assert.deepEqual(failures, []);
  console.log(JSON.stringify({answerCount:1, revision:graph.graph.revision, requests:requests.length, consoleErrors:errors, serverFailures:failures}));
} finally { await browser.close(); }
"""
    # stdin modules resolve Playwright from web/node_modules, with no test install.
    process = None
    try:
        wait_until(lambda: server.started)
        process = subprocess.Popen(
            [
                "node",
                "--input-type=module",
                "-",
                f"http://127.0.0.1:{port}",
                project_id,
                original["request"]["chat_id"],
                str(ready),
                str(initial_revision),
            ],
            cwd=Path(__file__).resolve().parents[1] / "web",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdin is not None
        process.stdin.write(node_source)
        process.stdin.close()
        wait_until(lambda: ready.exists() or process.poll() is not None, timeout=30)
        if ready.exists():
            finished.set()
            tasks._rearm_owed_transport_retries()
        process.wait(timeout=40)
        assert process.stdout is not None and process.stderr is not None
        output, error = process.stdout.read(), process.stderr.read()
        assert process.returncode == 0, error + output
        assert '"consoleErrors":[]' in output
        assert launcher.calls == 1
        assert app.state.service.history.state().revision == initial_revision + 1
        assert not [
            record for record in caplog.records if record.levelname in {"ERROR", "CRITICAL"}
        ]
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
