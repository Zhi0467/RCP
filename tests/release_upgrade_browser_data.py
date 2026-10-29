"""Copied team data for the release-upgrade browser journey, judged by the rehearsal first.

`web/tests/releaseUpgrade.browser.test.mjs` runs `python -m
tests.release_upgrade_browser_data <empty root>` to seed the deployment tests'
captured project, an ended Experiment on a display cache 0.4.34 wrote, and one
kept turn artifact. The update rehearsal must call that data ready first; one
JSON line names it. `... serve <root> <rcp serve arguments>` then serves it with
the fixture's server layout, as the deployment tests patch it.
"""

from __future__ import annotations

import json
import os
import pwd
import sys
import uuid
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rcp.storage.models as storage_models  # noqa: E402
from rcp.api import create_app  # noqa: E402
from rcp.server_ops.deployment import UpdateRehearsalRequest, update_rehearsal  # noqa: E402
from rcp.storage import AppStore  # noqa: E402
from tests.helpers import isolated_template_build  # noqa: E402
from tests.server_installed_upgrade import seed_turn_artifact  # noqa: E402
from tests.supervisor_reboot_data import prepare_data  # noqa: E402


def seed_experiment_on_old_display_cache(request, state, *, live: bool) -> None:
    """An Experiment run whose Runs row reads a cache 0.4.34 wrote, coach on its own machine."""
    from rcp.core.authority import AuthorizedHuman
    from rcp.service import RunRequest
    from rcp.storage import AgentTaskRecord

    project_id = state["project_id"]
    app = create_app(data_dir=Path(request.data_dir))
    store = app.state.catalog.store
    try:
        assert app.state.services.project_display_cache.rebuild_cached_snapshot(project_id)
        episode_id, operation_id, now = str(uuid.uuid4()), str(uuid.uuid4()), store.now()
        run = RunRequest(
            provider="codex",
            model="qualification",
            reasoning="medium",
            run_on="server",
            run_truth_scope=["paper"],
            chat_id=str(uuid.uuid4()),
            chat_scope="node",
            node_id="exp/recovery",
            mode="work",
            trigger="experiment_run",
            patch_kind="experiment_loop",
            control_node_id="exp/recovery",
            control_revision=1,
            control_episode_id=episode_id,
            control_invocation=1,
            control_invocation_ceiling=3,
            control_decision_bundle=[],
            control_completion_criteria=[],
        )
        store.create_experiment_episode_with_invocation(
            AgentTaskRecord(
                operation_id=operation_id,
                project_id=project_id,
                episode_id=episode_id,
                kind="node_chat",
                status="queued",
                request=run.model_dump(mode="json"),
                created_at=now,
                updated_at=now,
                status_message="Waiting for the loop invocation.",
                phase="queued",
                last_activity_at=now,
                authorized_by=AuthorizedHuman(
                    space_id=state["space_id"],
                    user_id=state["member_id"],
                    display_name="Qualification researcher",
                ),
            )
        )
        store.complete_agent_task(operation_id, applied_revision=None, result={})
        if not live:
            store.request_episode_stop(episode_id)
            store.mark_episode_stop_skipped(episode_id)
        cache = app.state.catalog._cached_snapshot_path(project_id)
    finally:
        store.close()
    envelope = json.loads(cache.read_text(encoding="utf-8"))
    envelope["snapshot"]["agent_profiles"]["paper_coach"]["run_on"] = "old-worker"
    cache.write_text(json.dumps(envelope), encoding="utf-8")


def _write_chat_transcript(research: Path, chat_id: str, operation_id: str) -> None:
    """The artifact turn's canonical chat file: one question and its answer."""
    record = {
        "sessionId": chat_id,
        "nodeId": None,
        "chatScope": "project",
        "operationId": operation_id,
    }
    lines = [
        {
            **record,
            "timestamp": "2026-10-01T12:00:00+00:00",
            "uuid": str(uuid.uuid4()),
            "type": role,
            "role": role,
            "text": text,
        }
        for role, text in (("user", "Draw the figure."), ("assistant", "Drew the figure."))
    ]
    (research / "chat").mkdir(exist_ok=True)
    (research / "chat" / f"project-{chat_id}.jsonl").write_text(
        "".join(json.dumps(line, separators=(",", ":")) + "\n" for line in lines),
        encoding="utf-8",
    )


def _fixture_layout(root: Path) -> SimpleNamespace:
    """Team projects live under the installed layout; this one is the fixture root."""
    account = pwd.getpwuid(os.geteuid()).pw_name
    return SimpleNamespace(service_account=account, projects_root=root / "projects")


def main(root: Path) -> None:
    root.mkdir(mode=0o700)
    root = root.resolve()
    data = root / "data"
    account = pwd.getpwuid(os.geteuid()).pw_name
    with isolated_template_build(root / "host") as monkeypatch:
        monkeypatch.setattr(storage_models, "DEFAULT_SERVER_LAYOUT", _fixture_layout(root))
        state = prepare_data(data, root / "projects", account=account, empty_branch=True)
        (data / "run-stage").chmod(0o700)
        seed_experiment_on_old_display_cache(SimpleNamespace(data_dir=str(data)), state, live=False)
        artifact = seed_turn_artifact(data, state, "browser")
        with closing(AppStore(data / "rcp.sqlite3")) as store:
            task = store.agent_task(artifact["operation_id"])
            assert task is not None
            chat_id = task.request["chat_id"]
            # Kept, so the project's saved artifacts list it.
            kept = store.keep_artifact(artifact["artifact_id"])
            store.mark_agent_artifact_kept(
                artifact["operation_id"], artifact["artifact_id"], kept_at=kept.kept_at
            )
        _write_chat_transcript(Path(state["research"]), chat_id, artifact["operation_id"])
        verdict = update_rehearsal(
            UpdateRehearsalRequest(
                version=1, data_dir=str(data), output_dir=str(root / "rehearsal")
            )
        )
    if verdict != {"version": 1, "status": "ready", "warnings": []}:
        raise SystemExit(f"The update rehearsal did not pass: {verdict}")
    print(
        json.dumps(
            {
                "data_dir": str(data),
                "token": state["token"],
                "project_id": state["project_id"],
                "artifact_id": artifact["artifact_id"],
                "chat_id": chat_id,
            }
        )
    )


def serve(root: Path, arguments: list[str]) -> None:
    from rcp.__main__ import main as rcp_main

    storage_models.DEFAULT_SERVER_LAYOUT = _fixture_layout(root.resolve())
    sys.argv = ["rcp", "serve", *arguments]
    rcp_main()


if __name__ == "__main__":
    if sys.argv[1] == "serve":
        serve(Path(sys.argv[2]), sys.argv[3:])
    else:
        main(Path(sys.argv[1]))
