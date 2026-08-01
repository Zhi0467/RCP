from __future__ import annotations

import pytest

from rcp.agents import validate_work_patch
from rcp.agents.prompts import PromptFactory
from tests.helpers import seed_patch


def _assert_pointer_envelope(prompt: str, contract_path: str) -> None:
    assert contract_path in prompt
    assert len(prompt.splitlines()) < 200
    assert "{" not in prompt
    assert "schema" not in prompt.casefold()
    assert "human request" not in prompt.casefold()
    assert "diagnostic" not in prompt.casefold()


def _assert_fixed_ontology_guidance(contract: str) -> None:
    assert "materialized project ontology" in contract
    assert "Use only active (non-deprecated) type, field, and relation" in contract
    assert "sets `extension_type` to the exact active custom\n  type name" in contract
    assert "puts only custom field values in\n  `extension_fields`" in contract
    assert "use its declared `kind`, include every required field" in contract
    assert "`agent_writable` value is false" in contract
    assert "complete desired OntologyState" in contract
    assert "`related_config_keys` is exactly\n  [`ontology`]" in contract
    assert "Never put `set_ontology` directly in the Patch" in contract
    assert "Every new Evidence must explicitly set `origin`" in contract
    assert "exact boundary is explicitly stated" in contract
    assert "Every `Hypothesis.status` transition needs a `cause`" in contract
    assert "There is no `unknown` cause" in contract
    assert "`human_edit` is reserved for human approval patches" in contract
    assert "`has_subquestion` ResearchQuestion->ResearchQuestion" in contract
    assert "`tests` Experiment->Hypothesis" in contract
    assert "`blocked_by` Experiment|Decision|ResearchQuestion->Blocker" in contract
    assert "`supersedes` and `duplicate_of` connect nodes of the same type" in contract
    assert "Never write a relation layer" in contract
    assert "confidence" not in contract.lower()


def test_launch_prompt_is_only_a_small_pointer_envelope() -> None:
    contract_path = "/tmp/rcp-run.example/inputs/task-op-initial.md"
    prompt = PromptFactory.launch_prompt(contract_path)

    _assert_pointer_envelope(prompt, contract_path)
    assert len(prompt.splitlines()) == 3


def test_graph_contract_keeps_fanout_and_points_to_payload_files() -> None:
    contract = PromptFactory.graph_task_contract(
        "refresh",
        project_name="Example",
        ontology_path="/state/graph.json#ontology",
        graph_path="/state/graph.json",
        research_path="/state/research.md",
        conversation_roots={"provider-x": "/stage/inputs/conversations/provider-x"},
        authorized_session_keys_path="/stage/inputs/authorized-session-keys.json",
        cursor_path="/state/cursors.json",
        repositories=[{"alias": "repo-a", "host": "", "path": "/repo-a"}],
        patch_path="/stage/workspace/patch.json",
        output_schema_path="/stage/inputs/patch-schema.json",
        human_request_path="/stage/inputs/human-request.txt",
        retry_diagnostics_path="/stage/inputs/retry-diagnostics.json",
    )

    assert "fan-out into bounded read-only specialists when it helps" in contract
    assert "sole writer of the final Patch" in contract
    assert "/stage/inputs/conversations/provider-x" in contract
    assert "/stage/inputs/authorized-session-keys.json" in contract
    assert "use only the exact `key` values from that file" in contract
    assert "Never derive a session key from a projected path" in contract
    assert "<provider-root>/<repository>/<machine>/<session-id>.jsonl" not in contract
    assert "/state/cursors.json" in contract
    assert "/state/graph.json#ontology" in contract
    assert "/stage/inputs/patch-schema.json" in contract
    assert "/stage/inputs/human-request.txt" in contract
    assert "/stage/inputs/retry-diagnostics.json" in contract
    assert "/stage/workspace/patch.json" in contract
    assert len(contract.splitlines()) < 200
    _assert_fixed_ontology_guidance(contract)


def test_work_contract_authorizes_exact_operations_and_an_optional_patch() -> None:
    contract = PromptFactory.work_task_contract(
        project_name="Example",
        ontology_path="/state/graph.json#ontology",
        graph_path="/state/graph.json",
        research_path="/state/research.md",
        focused_node_id="hyp/example",
        conversation_roots={"provider-x": "/stage/conversations/provider-x"},
        conversations_unreachable=0,
        repositories=[
            {"alias": "repo-a", "host": "", "path": "/repo-a"},
            {"alias": "repo-b", "host": "gpu.example", "path": "/srv/repo-b"},
        ],
        introduction_path=None,
        human_request_path="/stage/inputs/human-request.txt",
        patch_path="/stage/patch.json",
        artifact_path="/stage/artifacts",
        output_schema_path="/stage/inputs/patch-schema.json",
    )

    assert "independent Markdown reply" in contract
    assert "preview is optional" in contract
    assert "direct regular HTML or raster-image files" in contract
    assert "/state/graph.json" in contract
    assert "/stage/conversations/provider-x" in contract
    assert "/stage/inputs/human-request.txt" in contract
    assert "/stage/inputs/patch-schema.json" in contract
    assert "/stage/artifacts" in contract
    compact = " ".join(contract.split())
    assert "only graph-change channel RCP reads" in compact
    assert "Bash, network access, and SSH" in contract
    assert "only project locations you may change" in contract
    assert "host=`gpu.example` path=`/srv/repo-b`" in contract
    assert "do not copy the repository locally" in contract
    assert "Never create, edit, move, or delete `.research`" in contract
    assert "Patch absence is a normal successful Work result" in contract
    assert "one `work`/`agent` Patch JSON object" in contract
    assert "do not wrap every graph change in a Proposal" in contract
    assert "Set no coverage or cursors" in contract
    _assert_fixed_ontology_guidance(contract)


def test_discuss_contract_has_no_patch_path_or_schema_and_no_project_authority() -> None:
    contract = PromptFactory.discuss_task_contract(
        project_name="Example",
        ontology_path="/state/graph.json#ontology",
        graph_path="/state/graph.json",
        research_path="/state/research.md",
        focused_node_id=None,
        conversation_roots={},
        conversations_unreachable=0,
        repositories=[],
        introduction_path=None,
        human_request_path="/stage/inputs/human-request.txt",
        artifact_path="/stage/artifacts",
    )

    assert "no graph-change channel" in contract
    assert "Patch JSON Schema" not in contract
    assert "/stage/patch.json" not in contract
    assert "only place you may write" in contract
    assert "Never write canonical RCP state" in contract
    assert "Never copy, create, edit, or delete repository content" in contract
    assert "/stage/artifacts" in contract
    assert "Ontology authoring rules" not in contract


def test_paper_and_continuation_contracts_only_point_to_dynamic_content() -> None:
    paper = PromptFactory.paper_coach_task_contract(
        introduction_path="/state/paper/introduction.md",
        graph_path="/state/graph.json",
        research_path="/state/research.md",
        repositories=[{"alias": "repo-a", "host": "", "path": "/repo-a"}],
        human_request_path="/stage/inputs/human-request.txt",
        retry_diagnostics_path="/stage/inputs/retry.json",
    )
    correction = PromptFactory.continuation_task_contract(
        original_contract_path="/stage/inputs/task-initial.md",
        mode="patch_correction",
        patch_path="/stage/patch.json",
        diagnostics_path="/stage/inputs/correction.json",
    )

    assert "/state/paper/introduction.md" in paper
    assert "/stage/inputs/human-request.txt" in paper
    assert "/stage/inputs/retry.json" in paper
    assert "Never draft replacement sentences" in paper
    assert "/stage/inputs/correction.json" in correction
    assert "/stage/inputs/task-initial.md" in correction
    assert "Correct only the existing patch file" in correction
    assert "This continuation is not Work" in correction
    assert "Do not use network access, SSH, external services" in correction
    assert "rerun an experiment, resubmit a job, edit a repository" in correction
    compact_correction = " ".join(correction.split())
    assert "Do not re-read its repository or conversation inputs" in compact_correction
    assert "Any permission in the original contract to edit repositories" in correction
    assert "only confirm that the Patch was rewritten" in correction


def test_retry_handoff_contract_is_small_and_pointer_only() -> None:
    contract = PromptFactory.retry_handoff_task_contract(
        kind="seed",
        handoff_path="/stage/inputs/task-retry-handoff.json",
        original_contract_path="/prior/inputs/task-initial.md",
        patch_path="/stage/patch.json",
    )

    assert "/stage/inputs/task-retry-handoff.json" in contract
    assert "/prior/inputs/task-initial.md" in contract
    assert "/stage/patch.json" in contract
    assert "prior_progress_messages" not in contract
    assert "retained_patch" not in contract
    assert len(contract.splitlines()) < 20


def test_work_patch_legality_reuses_the_non_ingest_boundary_with_work_wording() -> None:
    cursor_patch = seed_patch().model_copy(
        update={"kind": "work", "processed_cursors": {"session": "record"}}
    )
    coverage_patch = seed_patch().model_copy(
        update={"kind": "work", "ops": [{"op": "set_coverage", "coverage": {}}]}
    )

    with pytest.raises(ValueError, match="A Work patch must not claim processed_cursors"):
        validate_work_patch(cursor_patch)
    with pytest.raises(ValueError, match="A Work patch must not set coverage"):
        validate_work_patch(coverage_patch)
