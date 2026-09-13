from __future__ import annotations

import inspect
import re

from rcp.agents.episode_report_prompt import episode_report_task_contract


def _paths(prompt: str) -> set[str]:
    return set(re.findall(r"`(/[^`]+)`", prompt))


def test_episode_report_contract_carries_supplied_paths_and_receipt_digest() -> None:
    receipt_path = "/stage/inputs/episode-receipt.json"
    skill_path = "/stage/packages/episode-report/SKILL.md"
    output_path = "/stage/workspace/episode-report.html"
    digest = "a" * 64

    prompt = episode_report_task_contract(
        project_name="Example",
        ending="completed",
        partial=False,
        receipt_path=receipt_path,
        receipt_sha256=digest,
        report_skill_path=skill_path,
        report_output_path=output_path,
    )

    assert _paths(prompt) == {receipt_path, skill_path, output_path}
    assert digest in prompt


def test_episode_report_contract_has_no_mode_or_large_context_parameters() -> None:
    parameters = set(inspect.signature(episode_report_task_contract).parameters)

    assert parameters == {
        "project_name",
        "ending",
        "partial",
        "receipt_path",
        "receipt_sha256",
        "report_skill_path",
        "report_output_path",
        "correction_diagnostic_path",
    }
    assert not parameters & {
        "mode",
        "surface",
        "graph_path",
        "research_path",
        "history_path",
        "repositories",
        "skill_pointers",
    }


def test_episode_report_correction_adds_only_its_diagnostic_pointer() -> None:
    receipt_path = "/stage/inputs/episode-receipt.json"
    skill_path = "/stage/packages/episode-report/SKILL.md"
    output_path = "/stage/workspace/episode-report.html"
    diagnostic_path = "/stage/inputs/report-diagnostic.txt"

    prompt = episode_report_task_contract(
        project_name="Example",
        ending="failed",
        partial=True,
        receipt_path=receipt_path,
        receipt_sha256="b" * 64,
        report_skill_path=skill_path,
        report_output_path=output_path,
        correction_diagnostic_path=diagnostic_path,
    )

    assert _paths(prompt) == {receipt_path, skill_path, output_path, diagnostic_path}
    assert prompt.count(diagnostic_path) == 1
