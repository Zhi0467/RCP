"""Awkward valid data, written by the same owners used by the application."""

from __future__ import annotations

import os
import pwd
import uuid
from pathlib import Path

from rcp.api import create_app
from rcp.artifacts import descriptor_for
from rcp.config import MachineConfig
from rcp.core.models import AuthorizedHuman
from rcp.runs.chat import project_chat_question_answer
from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.storage import AgentTaskRecord, AppStore, Artifact
from rcp.storage.question_models import QuestionOrigin
from tests.supervisor_reboot_data import prepare_data


def seed_real_use_corpus(data_dir: Path, projects_root: Path) -> dict:
    state = prepare_data(
        data_dir,
        projects_root,
        account=pwd.getpwuid(os.geteuid()).pw_name,
        project_name="sk-learn benchmarks",
    )
    store = AppStore(data_dir / "rcp.sqlite3")
    app = create_app(data_dir=data_dir)
    service = app.state.catalog.open(state["project_id"])
    service.history.add_machine(
        MachineConfig(
            alias="GPU_lab", host="gpu.example", provider_paths={"codex": "/tmp/sk-learn-exps"}
        )
    )
    actor = AuthorizedHuman(
        space_id=store.space_id,
        user_id=state["member_id"],
        display_name="Qualification researcher",
    )
    # Settle the seed's paused task so this same corpus can be transferred.
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        store.detach_agent_tasks_for_restore(
            connection, diagnostic="Corpus continuation settled", now=store.now()
        )
    operation_id = str(uuid.uuid4())
    run = RunRequest(
        provider="codex",
        run_on="server",
        chat_scope="project",
        chat_id=str(uuid.uuid4()),
        message="Which comparison?",
        mode="discuss",
    )
    task = store.create_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id=state["project_id"],
            kind="project_chat",
            status="queued",
            request=run.model_dump(mode="json"),
            created_at=store.now(),
            updated_at=store.now(),
            status_message="Queued",
            authorized_by=actor,
            dispatch_authority=resolve_dispatch_authority("project_chat", run),
        )
    )
    store.mark_agent_task_running(operation_id)
    store.checkpoint_agent_task(
        operation_id,
        native_session_id="corpus-session",
        stage_host="",
        stage_root=str(data_dir / "run-stage" / operation_id),
    )
    store.complete_agent_task(operation_id, applied_revision=None, result={})
    question = store.create_or_get_question(
        origin=QuestionOrigin(
            owner_kind="chat",
            project_id=task.project_id,
            owner_id=run.chat_id,
            operation_id=operation_id,
            provider="codex",
            native_session_id="corpus-session",
            stage_root=str(data_dir / "run-stage" / operation_id),
            stage_host="",
            capability="discuss",
            graph_target=task.graph_target,
        ),
        key="comparison",
        question="Which comparison?",
    )
    answered = store.answer_question(
        question.question_id, answer="Compare both.", resolved_by=actor
    )
    # The answer message names its follow-up turn in canonical chat history.
    project_chat_question_answer(service, store, answered)
    followup = store.admit_chat_question_followup(question.question_id)
    assert followup is not None
    name = "sk-learn_plot.png"
    data = b"\x89PNG\r\n\x1a\ncorpus"
    descriptor = descriptor_for(
        followup.operation_id, name, media_type="image/png", size_bytes=len(data)
    )
    artifact = store.create_artifact(
        Artifact(
            artifact_id=descriptor.artifact_id,
            project_id=task.project_id,
            supplier="turn",
            supplier_id=followup.operation_id,
            origin_operation_id=followup.operation_id,
            source_name=name,
            media_type="image/png",
            created_at=store.now(),
        ),
        data=data,
    )
    store.complete_agent_task(
        followup.operation_id,
        applied_revision=None,
        result={
            "artifacts": [descriptor.model_dump(mode="json")],
        },
    )
    kept = store.keep_artifact(artifact.artifact_id)
    store.mark_agent_artifact_kept(
        followup.operation_id, artifact.artifact_id, kept_at=kept.kept_at
    )
    state.update(operation_id=followup.operation_id, artifact_id=artifact.artifact_id, actor=actor)
    store.close()
    app.state.catalog.store.close()
    return state
