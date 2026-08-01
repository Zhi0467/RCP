from __future__ import annotations

import pytest

from rcp.config import load_manifest, write_project_scope
from rcp.core.models import Patch
from rcp.history import HistoryManager, PatchRejected
from rcp.paper import PaperService
from rcp.service import (
    GraphSyncRequest,
    ProjectService,
    ProposalDecisionRequest,
    ReviewRequest,
)
from rcp.storage import AppStore
from tests.helpers import seed_patch


def ontology_payload() -> dict[str, object]:
    return {
        "types": [
            {
                "name": "mechanism_hypothesis",
                "definition": "A hypothesis about the mechanism responsible for an effect.",
                "base_type": "hypothesis",
                "layer": "epistemic",
                "deprecated": False,
            }
        ],
        "fields": [],
        "relations": [],
    }


def proposal_patch() -> Patch:
    return Patch(
        kind="refresh",
        author="agent",
        summary="Proposed activating the replanning hypothesis.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "create_proposals",
                "proposals": [
                    {
                        "id": "prop/activate-replanning-hypothesis",
                        "title": "Treat replanning as the active hypothesis",
                        "card": {
                            "situation_cold": "The project has a proposed causal explanation but no active one.",
                            "why_human_now": "Activating it changes what experiments are interpreted against.",
                            "consequences": "Future evidence will be organized around this prediction.",
                            "decision_needed": "Decide whether the hypothesis is ready to become active.",
                        },
                        "ops": [
                            {
                                "op": "update_nodes",
                                "nodes": [
                                    {
                                        "id": "hyp/replanning-restores-plasticity",
                                        "changes": {"status": "active"},
                                        "cause": {
                                            "kind": "proposal_resolution",
                                            "ref_id": "prop/activate-replanning-hypothesis",
                                        },
                                    }
                                ],
                            }
                        ],
                        "related_node_ids": ["hyp/replanning-restores-plasticity"],
                        "base_rev": 1,
                    }
                ],
            }
        ],
    )


def ontology_proposal_patch() -> Patch:
    return Patch(
        kind="refresh",
        author="agent",
        summary="Proposed a project-specific ontology type.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "create_proposals",
                "proposals": [
                    {
                        "id": "prop/add-mechanism-hypothesis",
                        "title": "Add mechanism hypothesis type",
                        "card": {
                            "situation_cold": "Mechanism hypotheses need a distinct vocabulary.",
                            "why_human_now": "Ontology changes govern future graph authoring.",
                            "consequences": "New hypotheses may use this custom semantic type.",
                            "decision_needed": "Decide whether to activate the custom type.",
                        },
                        "ops": [{"op": "set_ontology", "ontology": ontology_payload()}],
                        "related_node_ids": [],
                        "related_config_keys": ["ontology"],
                        "base_rev": 1,
                    }
                ],
            }
        ],
    )


def test_approval_replays_exact_ops_and_accepts_node(manifest, tmp_path) -> None:
    history = HistoryManager(manifest)
    history.append(seed_patch())
    history.append(proposal_patch())
    service = ProjectService(
        manifest,
        history,
        PaperService(manifest, AppStore(tmp_path / "app.sqlite3")),
    )

    state = service.decide_proposal(
        "prop/activate-replanning-hypothesis",
        ProposalDecisionRequest(decision="approved"),
    )

    node = state.nodes["hyp/replanning-restores-plasticity"]
    assert node.status == "active"
    assert node.standing == "accepted"
    assert state.proposals["prop/activate-replanning-hypothesis"].status == "approved"


def test_agent_ontology_proposal_is_applied_only_after_human_approval(
    manifest, tmp_path
) -> None:
    history = HistoryManager(manifest)
    history.append(seed_patch())
    history.append(ontology_proposal_patch())
    assert history.state().ontology.types == []
    service = ProjectService(
        manifest,
        history,
        PaperService(manifest, AppStore(tmp_path / "app.sqlite3")),
    )

    state = service.decide_proposal(
        "prop/add-mechanism-hypothesis",
        ProposalDecisionRequest(decision="approved"),
    )

    assert [item.name for item in state.ontology.types] == ["mechanism_hypothesis"]
    assert state.config_revisions["ontology"] == 3
    assert state.proposals["prop/add-mechanism-hypothesis"].status == "approved"


def test_ontology_change_makes_an_older_ontology_proposal_stale(manifest, tmp_path) -> None:
    history = HistoryManager(manifest)
    history.append(seed_patch())
    history.append(ontology_proposal_patch())
    service = ProjectService(
        manifest,
        history,
        PaperService(manifest, AppStore(tmp_path / "app.sqlite3")),
    )
    service.sync_graph(
        GraphSyncRequest(base_revision=2, ontology=ontology_payload())
    )

    state = service.decide_proposal(
        "prop/add-mechanism-hypothesis",
        ProposalDecisionRequest(decision="approved"),
    )

    assert state.proposals["prop/add-mechanism-hypothesis"].status == "withdrawn"
    assert state.config_revisions["ontology"] == 3


def test_stale_proposal_is_withdrawn_without_replay(manifest, tmp_path) -> None:
    history = HistoryManager(manifest)
    history.append(seed_patch())
    history.append(proposal_patch())
    history.append(
        Patch(
            kind="refresh",
            author="agent",
            summary="Clarified the rationale after new discussion.",
            run_truth_scope=["repo-a"],
            repositories_read=["repo-a"],
            ops=[
                {
                    "op": "update_nodes",
                    "nodes": [
                        {
                            "id": "hyp/replanning-restores-plasticity",
                            "changes": {"rationale": "The mechanism is now framed more narrowly."},
                        }
                    ],
                }
            ],
        )
    )
    service = ProjectService(
        manifest,
        history,
        PaperService(manifest, AppStore(tmp_path / "app.sqlite3")),
    )

    state = service.decide_proposal(
        "prop/activate-replanning-hypothesis",
        ProposalDecisionRequest(decision="approved"),
    )

    assert state.proposals["prop/activate-replanning-hypothesis"].status == "withdrawn"
    assert state.nodes["hyp/replanning-restores-plasticity"].status == "proposed"


def test_proposal_cannot_claim_a_future_base_revision(manifest) -> None:
    history = HistoryManager(manifest)
    history.append(seed_patch())
    patch = proposal_patch()
    patch.ops[0]["proposals"][0]["base_rev"] = 999

    with pytest.raises(PatchRejected) as caught:
        history.append(patch)

    assert any(
        message.code == "proposal-base-revision" for message in caught.value.report.messages
    )
    assert history.state().proposals == {}


def test_proposal_cannot_omit_affected_nodes(manifest) -> None:
    history = HistoryManager(manifest)
    history.append(seed_patch())
    patch = proposal_patch()
    patch.ops[0]["proposals"][0].pop("related_node_ids")

    with pytest.raises(PatchRejected) as caught:
        history.append(patch)

    assert any(
        message.code == "proposal-dependency-mismatch"
        for message in caught.value.report.messages
    )
    assert history.state().proposals == {}


def test_agent_edge_touching_accepted_node_requires_and_accepts_proposal(
    manifest, tmp_path
) -> None:
    history = HistoryManager(manifest)
    history.append(seed_patch())
    service = ProjectService(
        manifest,
        history,
        PaperService(manifest, AppStore(tmp_path / "app.sqlite3")),
    )
    service.review_node(
        "rq/learning-after-shift",
        ReviewRequest(standing="accepted"),
    )
    patch = Patch(
        kind="refresh",
        author="agent",
        summary="Linked accepted content without human review.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "create_edges",
                "edges": [
                    {
                        "source": "rq/learning-after-shift",
                        "target": "hyp/replanning-restores-plasticity",
                        "relation": "supports",
                    }
                ],
            }
        ],
    )

    with pytest.raises(PatchRejected) as caught:
        history.append(patch)

    assert any(
        message.code == "accepted-edge-change" for message in caught.value.report.messages
    )
    assert (
        "rq/learning-after-shift::supports::hyp/replanning-restores-plasticity"
        not in history.state().edges
    )

    history.append(
        Patch(
            kind="refresh",
            author="agent",
            summary="Proposed linking accepted content.",
            run_truth_scope=["repo-a"],
            repositories_read=["repo-a"],
            ops=[
                {
                    "op": "create_proposals",
                    "proposals": [
                        {
                            "id": "prop/link-accepted-question",
                            "title": "Link the accepted question to the hypothesis",
                            "card": {
                                "situation_cold": "The relation is not represented.",
                                "why_human_now": "It changes accepted content.",
                                "consequences": "The graph will show direct support.",
                                "decision_needed": "Decide whether the relation is warranted.",
                            },
                            "ops": patch.ops,
                            "related_node_ids": [
                                "hyp/replanning-restores-plasticity",
                                "rq/learning-after-shift",
                            ],
                            "base_rev": history.state().revision,
                        }
                    ],
                }
            ],
        )
    )
    approved = service.decide_proposal(
        "prop/link-accepted-question",
        ProposalDecisionRequest(decision="approved"),
    )

    assert (
        "rq/learning-after-shift::supports::hyp/replanning-restores-plasticity"
        in approved.edges
    )


def test_proposal_with_unknown_repository_machine_is_rejected_at_creation(manifest) -> None:
    history = HistoryManager(manifest)
    history.append(seed_patch())
    before = manifest.path.read_text(encoding="utf-8")
    patch = Patch(
        kind="refresh",
        author="agent",
        summary="Proposed a repository on an unknown machine.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "create_proposals",
                "proposals": [
                    {
                        "id": "prop/add-invalid-repository",
                        "title": "Add an invalid repository",
                        "card": {
                            "situation_cold": "A repository might contain relevant evidence.",
                            "why_human_now": "Repository membership is guarded.",
                            "consequences": "Future agents would read this repository.",
                            "decision_needed": "Decide whether to add the repository.",
                        },
                        "ops": [
                            {
                                "op": "set_project_truth_scope",
                                "truth_scope": ["repo-a", "repo-b", "repo-c"],
                                "repository": {
                                    "alias": "repo-c",
                                    "machine": "missing-machine",
                                    "path": "/research/repo-c",
                                },
                            }
                        ],
                        "related_config_keys": ["project_truth_scope"],
                        "base_rev": 1,
                    }
                ],
            }
        ],
    )

    with pytest.raises(PatchRejected) as caught:
        history.append(patch)

    assert any(message.code == "invalid-proposal-ops" for message in caught.value.report.messages)
    assert manifest.path.read_text(encoding="utf-8") == before
    assert load_manifest(manifest.path).project.truth_scope == ["repo-a", "repo-b"]
    assert len(list((manifest.research_dir / "patches").glob("*.json"))) == 2


def test_manifest_scope_write_validates_before_replacing_file(manifest) -> None:
    before = manifest.path.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="unknown machine"):
        write_project_scope(
            manifest,
            ["repo-a", "repo-b", "repo-c"],
            repository_descriptor={
                "alias": "repo-c",
                "machine": "missing-machine",
                "path": "/research/repo-c",
            },
        )

    assert manifest.path.read_text(encoding="utf-8") == before
    assert load_manifest(manifest.path).project.truth_scope == ["repo-a", "repo-b"]


def test_proposal_replay_is_dry_run_materialized_at_creation(manifest) -> None:
    history = HistoryManager(manifest)
    history.append(seed_patch())
    patch = proposal_patch()
    raw_proposal = patch.ops[0]["proposals"][0]
    raw_proposal["ops"] = [
        {
            "op": "create_edges",
            "edges": [
                {
                    "source": "rq/learning-after-shift",
                    "target": "hyp/replanning-restores-plasticity",
                    "relation": "not-a-relation",
                }
            ],
        }
    ]
    raw_proposal["related_node_ids"] = [
        "hyp/replanning-restores-plasticity",
        "rq/learning-after-shift",
    ]

    with pytest.raises(PatchRejected) as caught:
        history.append(patch)

    assert any(message.code == "invalid-proposal-ops" for message in caught.value.report.messages)
    assert len(list((manifest.research_dir / "patches").glob("*.json"))) == 2


def test_valid_repository_proposal_can_still_be_approved(manifest, tmp_path) -> None:
    history = HistoryManager(manifest)
    history.append(seed_patch())
    history.append(
        Patch(
            kind="refresh",
            author="agent",
            summary="Proposed adding a valid repository.",
            run_truth_scope=["repo-a"],
            repositories_read=["repo-a"],
            ops=[
                {
                    "op": "create_proposals",
                    "proposals": [
                        {
                            "id": "prop/add-valid-repository",
                            "title": "Add a valid repository",
                            "card": {
                                "situation_cold": "A repository contains relevant evidence.",
                                "why_human_now": "Repository membership is guarded.",
                                "consequences": "Future agents may read this repository.",
                                "decision_needed": "Decide whether to add the repository.",
                            },
                            "ops": [
                                {
                                    "op": "set_project_truth_scope",
                                    "truth_scope": ["repo-a", "repo-b", "repo-c"],
                                    "repository": {
                                        "alias": "repo-c",
                                        "machine": "laptop",
                                        "path": str(tmp_path / "repo-c"),
                                    },
                                }
                            ],
                            "related_config_keys": ["project_truth_scope"],
                            "base_rev": 1,
                        }
                    ],
                }
            ],
        )
    )
    service = ProjectService(
        manifest,
        history,
        PaperService(manifest, AppStore(tmp_path / "app.sqlite3")),
    )

    state = service.decide_proposal(
        "prop/add-valid-repository",
        ProposalDecisionRequest(decision="approved"),
    )

    assert state.project_truth_scope == ["repo-a", "repo-b", "repo-c"]
    reloaded = load_manifest(manifest.path)
    assert reloaded.repository_map["repo-c"].machine == "laptop"
