from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress

import pytest

import rcp.config as config_module
from rcp.config import load_manifest
from rcp.core.models import Patch, ValidationMessage
from rcp.history import HistoryManager, PatchRejected, ReplayHalted, RevisionConflict
from tests.helpers import refresh_patch, seed_patch, shape_invalid_patch


def test_manifest_writes_share_the_append_lock_across_manager_instances(
    manifest, monkeypatch
) -> None:
    initial = HistoryManager(manifest)
    initial.append(seed_patch())
    initial.append(
        Patch(
            kind="refresh",
            author="agent",
            summary="Proposed narrowing the project truth scope.",
            run_truth_scope=["repo-a"],
            repositories_read=["repo-a"],
            ops=[
                {
                    "op": "create_proposals",
                    "proposals": [
                        {
                            "id": "prop/narrow-project-scope",
                            "title": "Narrow project scope",
                            "card": {
                                "situation_cold": "Repo-b is no longer part of this project.",
                                "why_human_now": "Project membership requires human review.",
                                "consequences": "Future runs will omit repo-b.",
                                "decision_needed": "Decide whether to remove repo-b.",
                            },
                            "ops": [
                                {
                                    "op": "set_project_truth_scope",
                                    "truth_scope": ["repo-a"],
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
    settings_history = HistoryManager(load_manifest(manifest.path))
    scope_history = HistoryManager(load_manifest(manifest.path))
    assert settings_history._process_lock is scope_history._process_lock

    real_atomic_write = config_module._atomic_write
    writes_ready = threading.Barrier(2)

    def synchronize_manifest_writes(path, content) -> None:
        if path == manifest.path:
            with suppress(threading.BrokenBarrierError):
                writes_ready.wait(timeout=0.25)
        real_atomic_write(path, content)

    monkeypatch.setattr(config_module, "_atomic_write", synchronize_manifest_writes)
    calls_ready = threading.Barrier(3)

    def update_provider_path() -> None:
        calls_ready.wait()
        settings_history.update_machine_provider_paths({"laptop": {"codex": "/opt/agents/codex"}})

    def change_scope() -> None:
        calls_ready.wait()
        scope_history.append(
            Patch(
                kind="approval",
                author="human",
                summary="Removed repo-b from the project truth scope.",
                ops=[
                    {
                        "op": "set_project_truth_scope",
                        "truth_scope": ["repo-a"],
                    },
                    {
                        "op": "resolve_proposals",
                        "resolutions": [{"id": "prop/narrow-project-scope", "status": "approved"}],
                    },
                ],
            )
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        settings_future = executor.submit(update_provider_path)
        scope_future = executor.submit(change_scope)
        calls_ready.wait()
        settings_future.result(timeout=5)
        scope_future.result(timeout=5)

    updated = load_manifest(manifest.path)
    assert updated.project.truth_scope == ["repo-a"]
    assert updated.machine_map["laptop"].provider_paths["codex"] == "/opt/agents/codex"
    assert scope_history.state().revision == 3


def test_seed_is_asserted_and_accepted_core_starts_empty(manifest) -> None:
    history = HistoryManager(manifest)
    forged = seed_patch().model_copy(
        update={
            "admission": "rejected",
            "admission_messages": [
                ValidationMessage(level="reject", code="forged", message="forged")
            ],
        }
    )
    patch, result = history.append(forged)

    assert patch.revision == 1
    assert patch.admission == "accepted"
    assert not patch.admission_messages
    assert history.load_patches()[0].admission == "accepted"
    assert result.state.revision == 1
    assert {node.standing for node in result.state.nodes.values()} == {"asserted"}
    assert (manifest.research_dir / "research.md").read_text(encoding="utf-8") == ""
    assert result.state.coverage.repositories_seen == ["repo-a"]
    assert result.state.coverage.repositories_never_seen == ["repo-b"]


def test_successful_patch_materializes_processed_cursors(manifest) -> None:
    history = HistoryManager(manifest)
    session_key = "repo-a/laptop/codex/session-1"

    history.append(seed_patch().model_copy(update={"processed_cursors": {session_key: "record-2"}}))

    cursors = json.loads((manifest.research_dir / "cursors.json").read_text(encoding="utf-8"))
    assert cursors == {session_key: "record-2"}


def test_standalone_review_generates_research_md(manifest) -> None:
    history = HistoryManager(manifest)
    history.append(seed_patch())
    history.append(
        Patch(
            kind="approval",
            author="human",
            summary="Accepted the primary question.",
            ops=[
                {
                    "op": "set_standing",
                    "node_id": "rq/learning-after-shift",
                    "standing": "accepted",
                }
            ],
        )
    )

    research = (manifest.research_dir / "research.md").read_text(encoding="utf-8")
    assert "Learning after task shift" in research
    assert "Replanning restores plasticity" not in research


@pytest.mark.parametrize("standing", ["asserted", "accepted", "contested"])
def test_direct_human_prose_edit_preserves_node_standing(manifest, standing) -> None:
    history = HistoryManager(manifest)
    history.append(seed_patch())
    if standing != "asserted":
        history.append(
            Patch(
                kind="approval",
                author="human",
                summary=f"Marked hypothesis {standing}.",
                ops=[
                    {
                        "op": "set_standing",
                        "node_id": "hyp/replanning-restores-plasticity",
                        "standing": standing,
                    }
                ],
            )
        )
    before = history.state().nodes["hyp/replanning-restores-plasticity"]

    patch, result = history.append(
        Patch(
            kind="approval",
            author="human",
            summary="Clarified the hypothesis wording.",
            ops=[
                {
                    "op": "update_nodes",
                    "nodes": [
                        {
                            "id": before.id,
                            "base_updated_rev": before.updated_rev,
                            "changes": {
                                "title": "Search-time replanning may preserve future learning",
                                "statement": (
                                    "Replanning during search may help the learner remain able "
                                    "to adapt after its task changes."
                                ),
                            },
                        }
                    ],
                }
            ],
        )
    )

    edited = result.state.nodes[before.id]
    assert edited.title == "Search-time replanning may preserve future learning"
    assert edited.standing.value == standing
    assert edited.updated_rev == patch.revision
    assert patch.ops == [
        {
            "op": "update_nodes",
            "nodes": [
                {
                    "id": before.id,
                    "base_updated_rev": before.updated_rev,
                    "changes": {
                        "title": "Search-time replanning may preserve future learning",
                        "statement": (
                            "Replanning during search may help the learner remain able "
                            "to adapt after its task changes."
                        ),
                    },
                }
            ],
        }
    ]


@pytest.mark.parametrize("field", ["status", "source_refs", "standing"])
def test_direct_human_edit_rejects_non_prose_fields(manifest, field) -> None:
    history = HistoryManager(manifest)
    history.append(seed_patch())
    node = history.state().nodes["hyp/replanning-restores-plasticity"]
    value = {
        "status": "active",
        "source_refs": [],
        "standing": "accepted",
    }[field]

    with pytest.raises(PatchRejected) as caught:
        history.append(
            Patch(
                kind="approval",
                author="human",
                summary="Tried to bypass direct-edit boundaries.",
                ops=[
                    {
                        "op": "update_nodes",
                        "nodes": [
                            {
                                "id": node.id,
                                "base_updated_rev": node.updated_rev,
                                "changes": {field: value},
                            }
                        ],
                    }
                ],
            )
        )

    assert any(
        message.code in {"non-prose-node-edit", "immutable-node-field"}
        for message in caught.value.report.messages
    )
    assert history.state().nodes[node.id].model_dump() == node.model_dump()


@pytest.mark.parametrize(
    ("operation", "code"),
    [
        (
            {
                "op": "update_nodes",
                "nodes": [
                    {
                        "id": "rq/learning-after-shift",
                        "base_updated_rev": 1,
                        "changes": {"title": "A clearer question"},
                    },
                    {
                        "id": "hyp/replanning-restores-plasticity",
                        "base_updated_rev": 1,
                        "changes": {"title": "A clearer hypothesis"},
                    },
                ],
            },
            "invalid-direct-node-edit",
        ),
        (
            {
                "op": "update_nodes",
                "nodes": [
                    {
                        "id": "hyp/replanning-restores-plasticity",
                        "changes": {"title": "Missing concurrency guard"},
                    }
                ],
            },
            "invalid-direct-node-edit",
        ),
        (
            {
                "op": "update_nodes",
                "nodes": [
                    {
                        "id": "hyp/replanning-restores-plasticity",
                        "base_updated_rev": 0,
                        "changes": {"title": "Stale edit"},
                    }
                ],
            },
            "stale-node-edit",
        ),
        (
            {
                "op": "update_nodes",
                "nodes": [
                    {
                        "id": "hyp/replanning-restores-plasticity",
                        "base_updated_rev": 1,
                        "changes": {"title": "Replanning restores plasticity"},
                    }
                ],
            },
            "empty-node-edit",
        ),
    ],
)
def test_malformed_direct_human_edit_shape_is_rejected(manifest, operation, code) -> None:
    history = HistoryManager(manifest)
    history.append(seed_patch())

    with pytest.raises(PatchRejected) as caught:
        history.append(
            Patch(
                kind="approval",
                author="human",
                summary="Malformed direct edit.",
                ops=[operation],
            )
        )

    assert any(message.code == code for message in caught.value.report.messages)


def test_agent_cannot_apply_gated_hypothesis_transition(manifest) -> None:
    history = HistoryManager(manifest)
    history.append(seed_patch())
    with pytest.raises(PatchRejected) as caught:
        history.append(
            Patch(
                kind="refresh",
                author="agent",
                summary="Changed hypothesis status directly.",
                run_truth_scope=["repo-a"],
                repositories_read=["repo-a"],
                ops=[
                    {
                        "op": "update_nodes",
                        "nodes": [
                            {
                                "id": "hyp/replanning-restores-plasticity",
                                "changes": {"status": "active"},
                            }
                        ],
                    }
                ],
            )
        )
    assert any(message.code == "gated-transition" for message in caught.value.report.messages)
    assert len(list((manifest.research_dir / "patches").glob("*.json"))) == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"standing": "accepted"},
        {"id": "hyp/renamed-behind-the-index"},
        {"type": "evidence"},
    ],
)
def test_node_updates_cannot_change_identity_or_standing(manifest, changes) -> None:
    history = HistoryManager(manifest)
    history.append(seed_patch())

    with pytest.raises(PatchRejected) as caught:
        history.append(
            Patch(
                kind="refresh",
                author="agent",
                summary="Tried to change a system-owned node field.",
                run_truth_scope=["repo-a"],
                repositories_read=["repo-a"],
                ops=[
                    {
                        "op": "update_nodes",
                        "nodes": [
                            {
                                "id": "hyp/replanning-restores-plasticity",
                                "changes": changes,
                            }
                        ],
                    }
                ],
            )
        )

    assert any(message.code == "immutable-node-field" for message in caught.value.report.messages)
    assert len(list((manifest.research_dir / "patches").glob("*.json"))) == 2


@pytest.mark.parametrize(
    ("operation", "code"),
    [
        (
            {
                "op": "supersede_nodes",
                "nodes": [
                    {
                        "id": "hyp/replanning-restores-plasticity",
                        "superseded_by": "rq/learning-after-shift",
                    }
                ],
            },
            "gated-transition",
        ),
        (
            {
                "op": "merge_nodes",
                "merges": [
                    {
                        "duplicate": "rq/learning-after-shift",
                        "canonical": "hyp/replanning-restores-plasticity",
                    }
                ],
            },
            "accepted-node-merge",
        ),
    ],
)
def test_agent_cannot_supersede_or_merge_accepted_nodes(manifest, operation, code) -> None:
    history = HistoryManager(manifest)
    history.append(seed_patch())
    history.append(
        Patch(
            kind="approval",
            author="human",
            summary="Accepted the hypothesis.",
            ops=[
                {
                    "op": "set_standing",
                    "node_id": "hyp/replanning-restores-plasticity",
                    "standing": "accepted",
                }
            ],
        )
    )

    with pytest.raises(PatchRejected) as caught:
        history.append(
            Patch(
                kind="refresh",
                author="agent",
                summary="Tried to rewrite accepted graph identity.",
                run_truth_scope=["repo-a"],
                repositories_read=["repo-a"],
                ops=[operation],
            )
        )

    assert any(message.code == code for message in caught.value.report.messages)
    assert len(list((manifest.research_dir / "patches").glob("*.json"))) == 3


@pytest.mark.parametrize(
    "operation",
    [
        {
            "op": "resolve_ambiguities",
            "resolutions": [{"id": "amb/missing", "status": "resolved"}],
        },
        {
            "op": "resolve_proposals",
            "resolutions": [{"id": "prop/missing", "status": "withdrawn"}],
        },
    ],
)
def test_unknown_resolution_target_is_rejected_before_append(manifest, operation) -> None:
    history = HistoryManager(manifest)
    history.append(seed_patch())

    with pytest.raises(PatchRejected):
        history.append(
            Patch(
                kind="refresh",
                author="agent",
                summary="Malformed resolution.",
                run_truth_scope=["repo-a"],
                repositories_read=["repo-a"],
                ops=[operation],
            )
        )

    assert len(list((manifest.research_dir / "patches").glob("*.json"))) == 2


def test_malformed_agent_patch_is_auditable_without_poisoning_replay(manifest) -> None:
    history = HistoryManager(manifest)
    history.append(seed_patch())
    malformed = Patch(
        kind="refresh",
        author="agent",
        summary="Malformed relation.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
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
        ],
    )

    appended, result = history.append(malformed, raise_on_reject=False)

    assert result.reports[appended.revision].rejected is True
    assert any(
        message.code == "invalid-edge" for message in result.reports[appended.revision].messages
    )
    stored = history.load_patches()[-1]
    assert appended.admission == stored.admission == "rejected"
    assert [message.code for message in stored.admission_messages] == ["invalid-edge"]
    replayed = history.state()
    assert replayed.replay_status == "complete"
    assert replayed.revision == appended.revision
    assert replayed.nodes["hyp/replanning-restores-plasticity"].status == "proposed"
    later, later_result = history.append(refresh_patch("rq/after-rejection"))
    assert later.revision == appended.revision + 1
    assert later_result.state.replay_status == "complete"
    assert "rq/after-rejection" in later_result.state.nodes


def test_discarded_rejection_does_not_enter_history_or_consume_revision(manifest) -> None:
    history = HistoryManager(manifest)

    with pytest.raises(PatchRejected) as caught:
        history.append(shape_invalid_patch(), discard_on_reject=True)

    assert caught.value.report.rejected is True
    assert history.load_patches() == []
    assert list((manifest.research_dir / "patches").glob("*.json")) == []
    accepted, result = history.append(seed_patch())
    assert accepted.revision == 1
    assert result.state.revision == 1


def test_tampered_accepted_patch_halts_before_it_and_blocks_later_writes(manifest) -> None:
    history = HistoryManager(manifest)
    history.append(seed_patch())
    history.append(refresh_patch("rq/tampered"))
    history.append(refresh_patch("rq/never-replayed"))

    path = manifest.research_dir / "patches" / "000002.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["ops"][0]["nodes"][0]["type"] = "not-a-node-type"
    path.write_text(json.dumps(raw), encoding="utf-8")

    state = history.state()

    assert state.replay_status == "degraded"
    assert state.revision == 1
    assert state.replay_failure is not None
    assert state.replay_failure.revision == 2
    assert state.replay_failure.code == "invalid-node"
    assert "rq/tampered" not in state.nodes
    assert "rq/never-replayed" not in state.nodes
    with pytest.raises(ReplayHalted, match="revision 2"):
        history.append(refresh_patch("rq/refused"))
    with pytest.raises(ReplayHalted, match="revision 2"):
        history.append_batch(
            [
                Patch(
                    kind="approval",
                    author="human",
                    summary="This write must be refused.",
                    ops=[],
                )
            ]
        )
    assert not (manifest.research_dir / "patches" / "000004.json").exists()


def test_patch_failing_part_way_leaks_no_earlier_operation(manifest) -> None:
    """A patch is all-or-nothing even when an earlier op in it already applied.

    `_fork_state` shares node objects between revisions and only copies the
    containers, so this is the property that keeps that sharing safe.
    """
    history = HistoryManager(manifest)
    history.append(seed_patch())
    before = history.state()
    partial = Patch(
        kind="refresh",
        author="agent",
        summary="Valid node followed by a malformed relation.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "create_nodes",
                "nodes": [
                    {
                        "id": "rq/transfer-after-shift",
                        "type": "research_question",
                        "title": "Transfer after task shift",
                        "question": "Does replanning transfer to an unseen task family?",
                        "motivation": "The seed corpus left transfer unexamined.",
                        "scope": "Matched compute across task families.",
                        "status": "open",
                    }
                ],
            },
            {
                "op": "create_edges",
                "edges": [
                    {
                        "source": "rq/transfer-after-shift",
                        "target": "hyp/replanning-restores-plasticity",
                        "relation": "not-a-relation",
                    }
                ],
            },
        ],
    )

    appended, result = history.append(partial, raise_on_reject=False)

    assert result.reports[appended.revision].rejected is True
    after = history.state()
    assert "rq/transfer-after-shift" not in after.nodes
    assert set(after.nodes) == set(before.nodes)
    assert set(after.edges) == set(before.edges)


def test_invalid_agent_patch_is_auditable_but_not_materialized(manifest) -> None:
    history = HistoryManager(manifest)
    history.append(seed_patch())
    patch = Patch(
        kind="refresh",
        author="agent",
        summary="Invalid gated transition.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "update_nodes",
                "nodes": [
                    {
                        "id": "hyp/replanning-restores-plasticity",
                        "changes": {"status": "supported"},
                    }
                ],
            }
        ],
    )
    appended, result = history.append(patch, raise_on_reject=False)

    assert appended.revision == 2
    assert (manifest.research_dir / "patches" / "000002.json").exists()
    node = result.state.nodes["hyp/replanning-restores-plasticity"]
    assert node.status == "proposed"
    assert any(message.code == "gated-transition" for message in result.state.validation_messages)
    graph = json.loads((manifest.research_dir / "graph.json").read_text(encoding="utf-8"))
    assert graph["nodes"]["hyp/replanning-restores-plasticity"]["status"] == "proposed"


def test_append_refuses_a_patch_written_against_a_moved_revision(manifest) -> None:
    """The freshness check has to happen where the write happens.

    A caller that checked first and appended second would leave a window for any
    other writer — a human Sync takes the append lock without ever taking the
    agent run lock.
    """

    history = HistoryManager(manifest)
    history.append(seed_patch())
    stale = refresh_patch("rq/written-against-revision-1")

    history.append(refresh_patch("rq/landed-first"))

    with pytest.raises(RevisionConflict):
        history.append(stale, expected_revision=1)
    assert history.state().revision == 2
    assert not (manifest.research_dir / "patches" / "000004.json").exists()
    assert "rq/written-against-revision-1" not in history.state().nodes
