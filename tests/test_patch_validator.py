from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from rcp.history import HistoryManager
from rcp.limits import WORK_PATCH_SELF_CHECK_MAX_COUNT
from rcp.paper import PaperService
from rcp.runs.patch_validator import (
    VALIDATOR_CLIENT_SOURCE,
    PatchValidationBudget,
    PatchValidationResult,
    serve_patch_validation_mailbox,
)
from rcp.runs.work import _apply_work_patch, _validate_work_patch_live
from rcp.service import ProjectService
from rcp.storage import AppStore
from tests.helpers import refresh_patch, seed_patch


class _RecordingStore:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str]] = []
        self.receipts: list[tuple[str, str, dict[str, object], str]] = []

    def record_agent_task_event(self, operation_id: str, message: str, *, level: str) -> None:
        self.events.append((operation_id, message, level))

    def record_agent_task_receipt(
        self,
        operation_id: str,
        category: str,
        payload: dict[str, object],
        *,
        tier: str,
    ) -> None:
        self.receipts.append((operation_id, category, payload, tier))


class _Execution:
    operation_id = "work-operation"

    def __init__(self) -> None:
        self.store = _RecordingStore()


async def _run_client(
    workspace: Path,
    client: Path,
    mailbox_id: str,
    *,
    timeout: float = 2,
) -> subprocess.CompletedProcess[str]:
    return await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            str(client),
            str(workspace / "patch.json"),
            mailbox_id,
            str(timeout),
            str(workspace),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.asyncio
async def test_validator_client_distinguishes_valid_invalid_and_unavailable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "patch.json").write_text("{}", encoding="utf-8")
    client = tmp_path / "validator-client.py"
    client.write_text(VALIDATOR_CLIENT_SOURCE, encoding="utf-8")

    for status, expected_code in (("valid", 0), ("invalid", 1)):
        mailbox_id = uuid.uuid4().hex
        stop = asyncio.Event()
        server = asyncio.create_task(
            serve_patch_validation_mailbox(
                mailbox_id=mailbox_id,
                workspace=workspace,
                remote_stage=None,
                execution=None,
                validate=lambda _text, status=status: PatchValidationResult(
                    status=status,
                    messages=[status],
                    live_revision=4,
                    candidate_revision=5,
                ),
                stop=stop,
                budget=PatchValidationBudget(),
            )
        )
        result = await _run_client(workspace, client, mailbox_id)
        stop.set()
        await server
        assert result.returncode == expected_code
        assert f'"status": "{status}"' in result.stdout

    unavailable = await _run_client(workspace, client, uuid.uuid4().hex, timeout=0.2)
    assert unavailable.returncode == 2
    assert "did not answer" in unavailable.stdout


@pytest.mark.asyncio
async def test_patch_self_checks_are_bounded_and_each_one_is_a_task_event(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "patch.json").write_text("{}", encoding="utf-8")
    client = tmp_path / "validator-client.py"
    client.write_text(VALIDATOR_CLIENT_SOURCE, encoding="utf-8")
    mailbox_id = uuid.uuid4().hex
    execution = _Execution()
    calls = 0

    def validate(_text: str) -> PatchValidationResult:
        nonlocal calls
        calls += 1
        return PatchValidationResult(status="valid", live_revision=1, candidate_revision=2)

    stop = asyncio.Event()
    budget = PatchValidationBudget()
    server = asyncio.create_task(
        serve_patch_validation_mailbox(
            mailbox_id=mailbox_id,
            workspace=workspace,
            remote_stage=None,
            execution=execution,  # type: ignore[arg-type]
            validate=validate,
            stop=stop,
            budget=budget,
        )
    )
    results = [
        await _run_client(workspace, client, mailbox_id)
        for _ in range(WORK_PATCH_SELF_CHECK_MAX_COUNT + 1)
    ]
    stop.set()
    await server

    assert [result.returncode for result in results[:-1]] == [0] * WORK_PATCH_SELF_CHECK_MAX_COUNT
    assert results[-1].returncode == 2
    assert calls == WORK_PATCH_SELF_CHECK_MAX_COUNT
    assert budget.count == WORK_PATCH_SELF_CHECK_MAX_COUNT + 1
    assert len(execution.store.events) == WORK_PATCH_SELF_CHECK_MAX_COUNT + 1
    assert "self-check limit" in results[-1].stdout


def test_live_self_check_and_apply_share_current_state_validation(manifest, tmp_path: Path) -> None:
    history = HistoryManager(manifest)
    history.append(seed_patch())
    service = ProjectService(
        manifest,
        history,
        PaperService(manifest, AppStore(tmp_path / "app.sqlite3")),
        data_dir=tmp_path,
    )
    semantic_patch = json.dumps(
        {
            "summary": "Create a Work result node.",
            "repositories_read": ["repo-a"],
            "change_summary": ["Created the Work result node."],
            "ops": [
                {
                    "op": "create_nodes",
                    "nodes": [
                        {
                            "id": "rq/live-validator-result",
                            "type": "research_question",
                            "title": "Live validator result",
                            "question": "Does the live validator see canonical movement?",
                        }
                    ],
                }
            ],
        }
    )

    checked = _validate_work_patch_live(
        service,
        semantic_patch,
        run_truth_scope=["repo-a"],
        patch_kind="work",
        control_node_id=None,
        control_decision_bundle=[],
    )
    assert checked.status == "valid"
    assert checked.live_revision == 1
    assert history.state().revision == 1

    history.append(refresh_patch("rq/live-validator-result"))
    rechecked = _validate_work_patch_live(
        service,
        semantic_patch,
        run_truth_scope=["repo-a"],
        patch_kind="work",
        control_node_id=None,
        control_decision_bundle=[],
    )
    assert rechecked.status == "invalid"
    assert rechecked.live_revision == 2
    assert any("already exists" in message for message in rechecked.messages)

    applied, failure = _apply_work_patch(
        service,
        None,
        semantic_patch,
        run_truth_scope=["repo-a"],
    )
    assert applied is None
    assert failure is not None
    assert "already exists" in failure.message
    assert history.state().revision == 2
