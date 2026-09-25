from __future__ import annotations

import hashlib
import json
import os
import pwd
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import uuid
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import get_args

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from pydantic_core import to_jsonable_python
from rcp_supervisor.checkpoint import (
    create_stopped_snapshot,
    restore_checkpoint,
)

import rcp.storage.models as storage_models
from rcp.api import create_app
from rcp.core.models import GraphState, upgrade_graph_projection
from rcp.server_ops.application_validation import _canonical_sha256
from rcp.server_ops.backup_capture import BackupCaptureCoordinator
from rcp.server_ops.backup_project_files import BackupProjectFileCaptureCoordinator
from rcp.server_ops.control import ServerControlPeer, ServerControlRequest
from rcp.server_ops.deployment import (
    ApplicationProof,
    PrepareRequest,
    ValidateRequest,
    _publish_proof,
    inventory,
    prepare,
    validate,
    verify_live_application,
)
from rcp.server_ops.maintenance import MaintenanceIdentity, MaintenanceRefused
from rcp.server_runtime import ServerMetadata
from rcp.storage import AppStore
from tests.supervisor_reboot_build import MIGRATION_TABLE, add_forward_migration
from tests.supervisor_reboot_data import prepare_data


@pytest.fixture
def socket_root():
    with tempfile.TemporaryDirectory(prefix="rcp-maint-", dir="/tmp") as directory:
        os.chown(directory, os.geteuid(), os.getegid())
        yield Path(directory)


@pytest.fixture
def captured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, socket_root: Path):
    account = pwd.getpwuid(os.geteuid()).pw_name
    monkeypatch.setattr(
        storage_models,
        "DEFAULT_SERVER_LAYOUT",
        SimpleNamespace(service_account=account, projects_root=tmp_path / "projects"),
    )
    state = prepare_data(tmp_path / "data", tmp_path / "projects", account=account)
    data = tmp_path / "data"
    (data / "run-stage").chmod(0o700)
    metadata = ServerMetadata.create(
        data,
        host="127.0.0.1",
        port=8421,
        owner_kind="cli",
        control_socket=socket_root / "control.sock",
        running_commit="a" * 40,
        web_build_id="sha256:" + "b" * 64,
    )
    capture = BackupCaptureCoordinator(
        AppStore(data / "rcp.sqlite3"), data, metadata
    ).capture_sqlite()
    assert capture.receipt.status == "complete"
    request = PrepareRequest(
        version=1,
        data_dir=str(data),
        output_dir=str(tmp_path / "prepared"),
        sqlite_receipt_path=str(capture.receipt_path),
        sqlite_receipt_sha256=capture.receipt_sha256,
    )
    return request, state, metadata


def _tree_state(root: Path) -> dict:
    entries = {}
    for current, directories, files in os.walk(root, followlinks=False):
        for path in [Path(current), *(Path(current) / name for name in directories + files)]:
            info = path.lstat()
            contents = (
                os.readlink(path)
                if stat.S_ISLNK(info.st_mode)
                else hashlib.sha256(path.read_bytes()).hexdigest()
                if stat.S_ISREG(info.st_mode)
                else None
            )
            entries[str(path.relative_to(root))] = (
                stat.S_IFMT(info.st_mode),
                stat.S_IMODE(info.st_mode),
                info.st_size if stat.S_ISREG(info.st_mode) else None,
                contents,
            )
    return entries


@pytest.mark.parametrize("failure", ["prepare", "verification"])
def test_real_project_payload_restores_schema_graph_stage_and_attachment(
    captured, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    request, state, metadata = captured
    data, research = Path(request.data_dir), Path(state["research"])
    # An agent run left links in its stage: one inside the stage, one to the host.
    stage = Path(state["stage"])
    (stage / "current").symlink_to("retained.txt")
    (stage / "python").symlink_to("/usr/bin/python3")
    (stage / "pytest-0").mkdir(mode=0o700)
    (stage / "pytest-current").symlink_to("pytest-0")  # pytest's directory link
    # uv installs agent packages as hardlinks into its cache and leaves a 0666 lock.
    (tmp_path / "uv-cache").mkdir(mode=0o700)
    (tmp_path / "uv-cache" / "pylab.py").write_text("from matplotlib.pylab import *\n")
    os.link(tmp_path / "uv-cache" / "pylab.py", stage / "pylab.py")
    (stage / ".lock").touch()
    (stage / ".lock").chmod(0o666)
    os.mkfifo(stage / "agent-pipe")
    (stage / "odd\\name\n").mkdir()
    (data / "unknown-agent-state").write_text("preserved by the supervisor")
    for relative, content in {
        "providers/claude/test-account/setup-token": "synthetic-token-do-not-publish",
        "jobs/completed/receipt.json": '{"status":"completed"}',
        f"run-stage/{stage.name}/unrecognized.future-file": "unknown retained bytes",
    }.items():
        path = data / relative
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(0o600)
    (data / "transfer-inbox").mkdir(mode=0o700, exist_ok=True)
    (research / "cursors.json").write_text('{"source":"retained-watermark"}')
    for name in ("facts", "paper"):
        (research / name).mkdir(mode=0o700, exist_ok=True)
        assert not list((research / name).iterdir())
    before = {root: _tree_state(root) for root in (data, research)}
    # Candidate root discovery must work before any old-code prepare and without
    # initializing even a read-only AppStore against the stopped installation.
    with monkeypatch.context() as guard:
        guard.setattr(AppStore, "__init__", lambda *args, **kwargs: pytest.fail("live AppStore"))
        guard.setattr(
            AppStore,
            "open_read_only_snapshot",
            lambda *args, **kwargs: pytest.fail("AppStore snapshot"),
        )
        discovered = inventory(request)
    assert not Path(request.output_dir).exists()
    assert [item["live"] for item in discovered["roots"]] == [str(data), str(research)]
    assert {root: _tree_state(root) for root in before} == before
    checkpoint = create_stopped_snapshot(
        tmp_path / "checkpoint",
        tuple(Path(root["live"]) for root in discovered["roots"]),
        boundary_sha256="d" * 64,
    )
    if failure == "prepare":

        def failed_prepare(*args, **kwargs):
            (stage / "retained.txt").write_text("partially settled preparation")
            raise MaintenanceRefused("Injected old prepare failure")

        with monkeypatch.context() as fault:
            fault.setattr("rcp.server_ops.deployment._check_copy", failed_prepare)
            with pytest.raises(MaintenanceRefused):
                prepare(request)
    else:
        prepared = prepare(request)
        assert {root["live"] for root in prepared["roots"]} == set(map(str, before))
        data_payload = Path(prepared["roots"][0]["payload"])
        assert not (data_payload / "providers").exists()
        assert not (data_payload / "jobs").exists()
        assert not (data_payload / "run-stage").exists()
        # Corrupting the old selected payload cannot affect the rollback source.
        shutil.rmtree(data_payload)
    with sqlite3.connect(data / "rcp.sqlite3") as connection:
        connection.execute("CREATE TABLE candidate_only (value TEXT)")
    (Path(state["research"]) / "candidate-only").write_text("discarded candidate state")
    (Path(state["stage"]) / "retained.txt").write_text("candidate altered")
    (stage / "current").unlink()
    (stage / "python").unlink()
    (stage / "python").symlink_to("/usr/bin/python3.99")
    restore_checkpoint(checkpoint)
    assert {root: _tree_state(root) for root in before} == before
    assert not (Path(state["research"]) / "candidate-only").exists()
    assert (Path(state["stage"]) / "retained.txt").read_text() != "candidate altered"
    assert os.readlink(stage / "current") == "retained.txt"
    assert os.readlink(stage / "python") == "/usr/bin/python3"
    assert os.readlink(stage / "pytest-current") == "pytest-0"
    restored = AppStore(data / "rcp.sqlite3")
    assert restored.authenticate_team_member_token(state["token"]).user_id == state["member_id"]
    with sqlite3.connect(data / "rcp.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='candidate_only'"
            ).fetchone()
            is None
        )
    fresh = BackupCaptureCoordinator(restored, data, metadata).capture_sqlite()
    backup = BackupProjectFileCaptureCoordinator(data).capture(
        fresh.receipt_path, expected_sha256=fresh.receipt_sha256
    )
    assert backup.receipt.status == fresh.receipt.status
    assert sum(project.status == "uncaptured" for project in backup.receipt.projects) == 0


def test_candidate_worker_crosses_real_forward_migration_without_touching_live(
    captured, tmp_path: Path
) -> None:
    request, state, _metadata = captured
    prepared = prepare(request)
    workspace = Path(__file__).resolve().parents[1]
    candidate = tmp_path / "candidate-code"
    shutil.copytree(
        workspace / "src" / "rcp",
        candidate / "rcp",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    head = add_forward_migration(candidate / "rcp" / "storage" / "base.py")
    worker_request = {
        "version": 1,
        "proof_path": prepared["proof_path"],
        "proof_sha256": prepared["proof_sha256"],
        "output_dir": str(tmp_path / "candidate-check"),
    }
    outcome = subprocess.run(
        [sys.executable, "-m", "rcp.server_ops.deployment", "validate", "-"],
        input=json.dumps(worker_request),
        env={**os.environ, "PYTHONPATH": str(candidate)},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert outcome.returncode == 0, outcome.stderr
    result = json.loads(outcome.stdout)
    assert result["status"] == "verified"
    migrated = tmp_path / "candidate-check" / "candidate" / "overlay" / "data" / "rcp.sqlite3"
    with sqlite3.connect(migrated) as connection:
        assert (
            connection.execute(
                "SELECT MAX(migration_version) FROM storage_schema_migrations"
            ).fetchone()[0]
            == head
        )
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name=?", (MIGRATION_TABLE,)
        ).fetchone()
    assert (
        AppStore(Path(request.data_dir) / "rcp.sqlite3").storage_schema_ledger_head()
        == state["ledger_head"]
    )


def test_changed_proof_and_existing_output_fail_closed(captured, tmp_path: Path) -> None:
    request, _state, _metadata = captured
    prepared = prepare(request)
    proof = Path(prepared["proof_path"])
    proof.write_bytes(proof.read_bytes() + b" ")
    with pytest.raises(MaintenanceRefused):
        validate(
            ValidateRequest(
                version=1,
                proof_path=str(proof),
                proof_sha256=prepared["proof_sha256"],
                output_dir=str(tmp_path / "should-not-exist"),
            )
        )
    assert not (tmp_path / "should-not-exist").exists()


def test_projection_defaults_have_explicit_recursive_upgrade_examples() -> None:
    """Cover every model path and independently omit every defaulted field.

    Keep the literal schema inventory independent of model validation/serialization;
    separately verify the real serialized Proposal shape.
    """
    examples = json.loads(
        """
        {
          "source": {"machine": "local", "truth_repository": "research", "source": "codex", "session_id": "session", "record_uuid": "record", "timestamp": "2026-01-01T00:00:00Z", "excerpt": "claim"},
          "common": {"title": "Example", "extension_type": null, "extension_fields": {"label": "kept"}, "standing": "accepted", "created_rev": 1, "updated_rev": 2},
          "nodes": {
            "question": {"type": "research_question", "question": "Why?", "motivation": "context", "scope": "bounded", "status": "open"},
            "hypothesis": {"type": "hypothesis", "statement": "A claim", "rationale": "reason", "predictions": ["result"], "scope": "bounded", "status": "active"},
            "decision": {"type": "decision", "question": "Which?", "options": ["one", "two"], "selected_option": "one", "rationale": "reason", "consequences": ["change"], "status": "decided"},
            "experiment": {"type": "experiment", "objective": "test", "design": "protocol",
              "proxies": [{"stands_for": "quantity", "measure": "observation"}], "limitations": ["bounded"],
              "expected_outcomes": ["result"], "interpretation_rules": ["rule"], "completion_criteria": ["done"],
              "invocation_ceiling": 7, "status": "completed",
              "attempts": [{"id": "attempt", "sequence": 1, "purpose": "test", "attempt_kind": "external_run",
              "decision_bundle": [{"decision_id": "decision", "decision_revision": 2, "selected_option": "one"}],
              "debug": {"mechanical_fault": "fault", "change": "repair", "predicted_effect": "works"},
              "configuration": "config", "status": "completed", "job_refs": ["job"], "outcome": "result", "failure_reason": null,
              "started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:00:00Z"}],
              "current_summary": "done", "next_action": "review", "current_summary_stale": true, "next_action_stale": true},
            "evidence": {"type": "evidence", "observation": "result", "interpretation": "finding", "role": "result", "legacy_strength": null, "validity": "qualified", "origin": "internal_run", "artifact_refs": ["result.txt"]},
            "blocker": {"type": "blocker", "description": "obstacle", "blocker_type": "data", "status": "open", "resolution_condition": "available", "recommended_action": "collect"}
          },
          "edge": {"id": "edge", "source": "evidence", "target": "hypothesis", "relation": "supports", "layer": "epistemic", "explanation": "result", "assessment": {"relevance": "direct", "weight": "strong", "scope": "bounded", "qualifications": ["caveat"]}, "expectation": "diverged", "created_rev": 2},
          "ontology": {
            "types": [{"name": "special", "definition": "specialized", "base_type": "hypothesis", "layer": "epistemic", "deprecated": false}],
            "fields": [{"owner_type": "special", "name": "label", "definition": "label", "kind": "text", "required": false, "agent_writable": true, "deprecated": false}],
            "relations": [{"name": "custom", "definition": "custom", "source_types": ["special"], "target_types": ["hypothesis"], "layer": "epistemic", "deprecated": false}]
          },
          "ambiguity": {"id": "ambiguity", "question": "Which?", "why_it_matters": "reason", "candidates": ["one"], "related_node_ids": ["question"], "artifact_refs": ["result.txt"], "status": "open"},
          "term": {"term": "term", "plain_definition": "meaning", "where_defined": "result.txt"},
          "coverage": {"repositories_seen": ["research"], "repositories_never_seen": ["other"], "sessions_read": ["session"], "sessions_skipped": ["other"], "earliest_timestamp": "2026-01-01T00:00:00Z", "note": "report"},
          "current": {
            "revision": 2,
            "project_truth_scope": ["research"],
            "config_revisions": {"research": 1},
            "proposals": {"proposal": {"id": "proposal", "title": "change",
              "card": {"situation_cold": "context", "why_human_now": "choice", "consequences": "change", "decision_needed": "approve"},
              "related_node_ids": ["hypothesis"], "related_edge_ids": ["edge"], "related_config_keys": ["research"],
              "base_rev": 1, "status": "pending", "created_by": "agent", "created_by_operation_id": "operation",
              "raised_rev": 2, "resolved_rev": null, "resolved_by": null, "resolved_by_operation_id": null,
              "resolution_reason": null, "rejection_reason": null}},
            "ambiguities": {"ambiguity": {"id": "ambiguity", "question": "Which?", "why_it_matters": "reason", "candidates": ["one"], "related_node_ids": ["question"], "artifact_refs": ["result.txt"], "status": "open", "raised_rev": 1}},
            "glossary": {"term": {"term": "term", "plain_definition": "meaning", "where_defined": "result.txt", "updated_rev": 2}},
            "validation_messages": [{"level": "flag", "code": "example", "message": "message", "patch_revision": 2, "related_node_ids": ["hypothesis"], "related_edge_ids": ["edge"], "operation_index": 0, "rule_id": "rule", "cause_chain": [{"code": "cause"}], "failed_invariant": "example"}],
            "belief_transitions": [{"hypothesis_id": "hypothesis", "from_status": "proposed", "to_status": "active", "revision": 2, "cause": {"kind": "evidence_edge", "ref_id": "ref"}}],
            "replay_status": "degraded",
            "replay_failure": {"revision": 2, "created_at": "2026-01-01T00:00:00Z", "code": "example", "message": "failure"},
            "last_refresh_at": "2026-01-01T00:00:00Z"
          }
        }
        """
    )
    source, common, nodes = examples["source"], examples["common"], examples["nodes"]
    nodes = {key: dict(common, id=key, source_refs=[source], **node) for key, node in nodes.items()}
    nodes["experiment"]["attempts"][0]["source_refs"] = [source]
    edge, ontology = examples["edge"], examples["ontology"]
    ambiguity, term, coverage = examples["ambiguity"], examples["term"], examples["coverage"]
    causes = [
        dict(kind=kind, ref_id="ref")
        for kind in ("evidence_edge", "decision", "proposal_resolution")
    ]
    causes.append(dict(kind="human_edit"))
    updates = [
        dict(id="hypothesis", changes={"title": "changed"}, cause=cause, base_updated_rev=2)
        for cause in causes
    ]
    supersedes = [
        dict(id="hypothesis", superseded_by="other", explanation="reason", cause=causes[0])
    ]
    merges = [
        dict(duplicate="hypothesis", canonical="other", explanation="reason", cause=causes[0])
    ]
    new_edge = {key: value for key, value in edge.items() if key not in {"layer", "created_rev"}}
    operations = [
        dict(op="update_nodes", intent="content_change", nodes=updates),
        dict(op="update_nodes", intent="status_change", nodes=updates),
        dict(
            op="set_standing", intent="standing_change", node_id="hypothesis", standing="accepted"
        ),
        dict(op="remove_nodes", intent="removal", node_ids=["hypothesis"]),
        dict(op="supersede_nodes", intent="supersede", nodes=supersedes),
        dict(op="merge_nodes", intent="merge", merges=merges),
        dict(
            op="create_edges", intent="protected_relation_change", edges=[new_edge], edge_ids=None
        ),
        dict(op="remove_edges", intent="protected_relation_change", edges=None, edge_ids=["edge"]),
    ]
    operations += [
        dict(operation, intent="legacy_" + operation["intent"])
        for operation in operations
        if operation["intent"] != "standing_change"
    ]
    operations += [
        dict(
            op="set_project_truth_scope",
            intent="legacy_project_truth_scope_change",
            truth_scope=["research"],
            repository=dict(alias="research", machine="local", path="/workspace/research"),
        ),
        dict(op="set_ontology", intent="legacy_ontology_change", ontology=ontology),
        dict(op="create_nodes", intent="legacy_create_nodes", nodes=list(nodes.values())),
        dict(op="create_ambiguities", intent="legacy_create_ambiguities", ambiguities=[ambiguity]),
        dict(
            op="resolve_ambiguities",
            intent="legacy_resolve_ambiguities",
            resolutions=[dict(id="ambiguity", status="resolved")],
        ),
        dict(op="upsert_glossary", intent="legacy_upsert_glossary", terms=[term]),
        dict(op="set_coverage", intent="legacy_set_coverage", coverage=coverage),
    ]
    current = examples["current"]
    current.update(
        nodes=nodes,
        ontology=ontology,
        edges={
            "edge": edge,
            # Materialization resolves this declared action relation to a seam.
            "blocked": dict(
                edge,
                id="blocked",
                source="question",
                target="blocker",
                relation="blocked_by",
                layer="seam",
                assessment=None,
            ),
        },
    )
    current["proposals"]["proposal"]["ops"] = operations
    # Stored JSON has no shared object identities between repeated examples.
    current = json.loads(json.dumps(current))
    reachable = set()
    unsafe_defaults = []

    def discover(annotation):
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            if annotation not in reachable:
                reachable.add(annotation)
                for name, field in annotation.model_fields.items():
                    factory = field.default_factory
                    if factory is not None and factory not in (list, dict, tuple, set, frozenset):
                        if isinstance(factory, type) and issubclass(factory, BaseModel):
                            discover(factory)
                        else:
                            unsafe_defaults.append(f"{annotation.__name__}.{name}")
                    discover(field.annotation)
        else:
            for argument in get_args(annotation):
                discover(argument)

    discover(GraphState)
    assert not unsafe_defaults, (
        "Defaults require explicit migration (unproven deterministic factory): "
        + ", ".join(sorted(unsafe_defaults))
    )
    covered = set()
    upgrades = []

    def check_paths(model, document, path=()):
        if isinstance(model, BaseModel):
            covered.add(type(model))
            assert set(document) == set(type(model).model_fields), (
                f"{path}: missing explicit field/upgrade example"
            )
            for name, field in type(model).model_fields.items():
                if not field.is_required():
                    default = to_jsonable_python(field.get_default(call_default_factory=True))
                    upgrades.append(((*path, name), default))
                check_paths(getattr(model, name), document[name], (*path, name))
        elif isinstance(model, dict):
            for key, value in model.items():
                check_paths(value, document[key], (*path, key))
        elif isinstance(model, list):
            for index, value in enumerate(model):
                check_paths(value, document[index], (*path, index))

    check_paths(GraphState.model_validate(deepcopy(current)), current)
    assert covered == reachable, f"Missing populated examples: {reachable - covered}"
    # Every defaulted field is omitted independently, preserving all other
    # explicit values. Model-derived defaults are the only expected additions.
    for path, default in upgrades:
        legacy = deepcopy(current)
        expected = deepcopy(current)
        old_parent, expected_parent = legacy, expected
        for component in path[:-1]:
            old_parent, expected_parent = old_parent[component], expected_parent[component]
        del old_parent[path[-1]]
        expected_parent[path[-1]] = default
        upgrade_graph_projection(legacy)
        assert legacy == expected, path
        upgrade_graph_projection(legacy)
        assert legacy == expected, path
    # Real Proposal serialization omits legacy intents, including create_nodes
    # with nested Experiments. Compare that stored shape with a fresh serialization.
    graph = GraphState.model_validate(current)
    operation = next(op for op in graph.proposals["proposal"].ops if op.op == "create_nodes")
    experiment = next(node for node in operation.nodes if node.type == "experiment")
    experiment.invocation_ceiling = type(experiment).model_fields["invocation_ceiling"].default
    serialized = graph.model_dump(mode="json")
    legacy = deepcopy(serialized)
    operation = next(
        op for op in legacy["proposals"]["proposal"]["ops"] if op["op"] == "create_nodes"
    )
    assert "intent" not in operation
    experiment = next(node for node in operation["nodes"] if node["type"] == "experiment")
    del experiment["invocation_ceiling"]
    upgrade_graph_projection(legacy)
    assert legacy == serialized
    upgrade_graph_projection(legacy)
    assert legacy == serialized
    current["future_graph_field"] = {"kept": True}
    current["nodes"]["experiment"]["future_node_field"] = "kept"
    current["edges"]["edge"]["assessment"]["scope"] = "  unnormalized  "
    preserved = deepcopy(current)
    preserved["coverage"] = coverage
    upgrade_graph_projection(preserved)
    assert preserved == current  # Includes populated defaults and the stored edge layers.
    upgrade_graph_projection(preserved)
    assert preserved == current


@pytest.mark.parametrize("failure", [None, "changed_graph", "changed_startup", "tampered_graph"])
def test_upgrade_accepts_only_authenticated_known_projection_changes(
    captured, tmp_path: Path, failure: str | None
) -> None:
    request, _state, _metadata = captured
    prepared = prepare(request)
    proof_path = Path(prepared["proof_path"])
    proof = ApplicationProof.model_validate_json(proof_path.read_bytes())
    capture = proof.project_receipt.projects[0]
    graph_path = (
        proof_path.parent
        / "baseline/overlay/projects"
        / capture.project_id
        / "repositories"
        / capture.recovery.configuration.state_repository
        / ".research/graph.json"
    )
    graph = json.loads(graph_path.read_text())
    # Reproduce the previous release's projection and authenticated proof. This
    # changes only disposable worker output, never the captured canonical input.
    graph["coverage"] = {
        "repositories_seen": ["research"],
        "repositories_never_seen": [],
        "sessions_read": ["old-session"],
        "sessions_skipped": [],
        "earliest_timestamp": None,
        # A graph past the 4 MiB request bound is still an ordinary graph.
        "note": "Historical reading report." + " " * (4 * 1024 * 1024),
    }
    # It also omitted fields later added with empty defaults.
    for edge in graph["edges"].values():
        edge.pop("expectation")
    experiments = [node for node in graph["nodes"].values() if node["type"] == "experiment"]
    for node in experiments:
        node.pop("proxies")
        node.pop("limitations")
    assert graph["edges"] and experiments
    if failure == "changed_graph":
        graph["nodes"] = {}
    graph_path.write_text(json.dumps(graph))
    projects = tuple(
        project.model_copy(update={"projection_sha256": _canonical_sha256(graph)})
        if project.project_id == capture.project_id
        else project
        for project in proof.read_model.projects
    )
    read_model = proof.read_model.model_copy(update={"projects": projects})
    if failure == "changed_startup":
        startup = read_model.startup_recovery.model_copy(
            update={
                "active_operation_ids": (*read_model.startup_recovery.active_operation_ids, "other")
            }
        )
        read_model = read_model.model_copy(update={"startup_recovery": startup})
    proof = proof.model_copy(update={"read_model": read_model})
    proof_path.unlink()
    digest = _publish_proof(proof_path, proof)
    if failure == "tampered_graph":
        graph["coverage"]["note"] = "Changed after the proof was signed."
        graph_path.write_text(json.dumps(graph))
    validation = ValidateRequest(
        version=1,
        proof_path=str(proof_path),
        proof_sha256=digest,
        output_dir=str(tmp_path / "validated"),
    )
    if failure:
        with pytest.raises(MaintenanceRefused):
            validate(validation)
        assert not (tmp_path / "validated/application-proof.json").exists()
    else:
        checked = validate(validation)
        assert checked["status"] == "verified"
        current = ApplicationProof.model_validate_json(Path(checked["proof_path"]).read_bytes())
        graph.pop("coverage")
        for edge in graph["edges"].values():
            edge["expectation"] = None
        for node in experiments:
            node.update(proxies=[], limitations=[])
        assert current.read_model.projects[0].projection_sha256 == _canonical_sha256(graph)


@pytest.mark.parametrize("case", ["local", "remote", "changed"])
def test_live_check_opens_a_remote_project_only_after_the_release_commits(
    captured, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    import rcp.server_ops.deployment as deployment

    request, _state, _metadata = captured
    prepared = prepare(request)
    checked = validate(
        ValidateRequest(
            version=1,
            proof_path=prepared["proof_path"],
            proof_sha256=prepared["proof_sha256"],
            output_dir=str(tmp_path / "validated"),
        )
    )
    data = Path(request.data_dir)
    shutil.rmtree(data / "project-snapshots", ignore_errors=True)
    live = create_app(data_dir=data)
    store = live.state.background_tasks.store
    if case == "remote":
        # The capture classified this project remote; the live row agrees.
        locate = deployment._project_restore_location
        monkeypatch.setattr(deployment, "_project_restore_location", lambda p: (locate(p)[0], None))
    if case != "local":
        project = store.project
        monkeypatch.setattr(
            store,
            "project",
            lambda project_id: project(project_id).model_copy(update={"state_remote": True}),
        )
    opened = []
    open_snapshot = live.state.catalog.open_snapshot
    monkeypatch.setattr(
        live.state.catalog,
        "open_snapshot",
        lambda project_id: opened.append(project_id) or open_snapshot(project_id),
    )

    def verify() -> str:
        return verify_live_application(
            Path(checked["proof_path"]),
            proof_sha256=checked["proof_sha256"],
            background=live.state.background_tasks,
            catalog=live.state.catalog,
            store=store,
        )

    if case == "changed":
        # A candidate migration that reroutes a captured local project is refused.
        with pytest.raises(MaintenanceRefused):
            verify()
        return
    assert verify()
    # A missing cache is rebuilt for a local project, never opened for a remote one.
    assert bool(opened) is (case == "local")


def test_explicit_probe_stays_fenced_until_matching_app_proof(captured, tmp_path: Path) -> None:
    request, _state, metadata = captured
    prepared = prepare(request)
    boundary = MaintenanceIdentity(str(uuid.uuid4()), "c" * 64)
    app = create_app(
        data_dir=Path(request.data_dir), instance_metadata=metadata, maintenance_identity=boundary
    )
    control = app.state.server_control
    assert control is not None

    def command(operation: str, **kwargs):
        return control.handler(
            ServerControlRequest(
                request_id=str(uuid.uuid4()),
                instance_id=metadata.instance_id,
                operation=operation,
                selector_id=boundary.maintenance_id,
                boundary_sha256=boundary.boundary_sha256,
                **kwargs,
            ),
            ServerControlPeer(pid=os.getpid(), uid=0, gid=0),
        )

    # Running the lifespan opens its disposable local socket but leaves every
    # ordinary runtime owner asleep until the verified fence release.
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 503
        assert command("maintenance_status").quiescent
        assert not app.state.startup_effect_runtime_started
        with pytest.raises(RuntimeError, match="verification"):
            command("maintenance_release")
        verified = command(
            "maintenance_verify",
            proof_path=prepared["proof_path"],
            proof_sha256=prepared["proof_sha256"],
        )
        assert len(verified.verification_sha256) == 64
        assert client.get("/api/health").status_code == 503
        released = command("maintenance_release")
        assert not released.closed
        assert client.get("/api/health").status_code == 200
        assert app.state.startup_effect_runtime_started


def test_capabilities_never_opens_data(tmp_path: Path) -> None:
    data = tmp_path / "must-not-exist"
    completed = subprocess.run(
        [sys.executable, "-m", "rcp.server_ops.deployment", "capabilities"],
        env={**os.environ, "RCP_DATA_DIR": str(data)},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "version": 1,
        "maintenance_protocol": 10,
        "commands": [
            "inventory",
            "prepare",
            "validate",
            "inspect",
            "offline-inventory",
            "offline-prepare",
            "restore-prepare",
            "offline-protect",
        ],
    }
    assert not data.exists()


@pytest.mark.parametrize("mutation", ["missing_local_project", "existing_output"])
def test_prepare_refuses_incomplete_or_unsafe_local_boundary(
    captured, tmp_path: Path, mutation: str
) -> None:
    request, state, _metadata = captured
    if mutation == "missing_local_project":
        research = Path(state["research"])
        research.rename(research.with_name("held-research"))
    else:
        output = Path(request.output_dir)
        output.mkdir()
        (output / "sentinel").write_text("preserve")
    with pytest.raises((RuntimeError, ValueError, OSError)):
        prepare(request)
    assert not (Path(request.output_dir) / "application-proof.json").exists()
    if mutation == "existing_output":
        assert (Path(request.output_dir) / "sentinel").read_text() == "preserve"


def test_trusted_deployed_commit_is_bound_to_wheel_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rcp.server_runtime import ServerMetadataError, capture_installed_release_identity

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "index.html").write_text("packaged app")
    monkeypatch.setattr("rcp.__version__", "0.3.4+build.123.gabcdef0")
    monkeypatch.setattr("rcp.web_assets.web_dist_path", lambda: bundle)
    monkeypatch.setenv("RCP_DEPLOYED_COMMIT", "abcdef0" + "1" * 33)
    identity = capture_installed_release_identity()
    assert identity.commit == "abcdef0" + "1" * 33
    assert identity.web_build_id.startswith("sha256:")
    monkeypatch.setenv("RCP_DEPLOYED_COMMIT", "fedcba0" + "1" * 33)
    with pytest.raises(ServerMetadataError):
        capture_installed_release_identity()


def test_running_service_closes_drains_captures_and_releases_maintenance(
    captured, monkeypatch
) -> None:
    request, _state, metadata = captured
    app = create_app(data_dir=Path(request.data_dir), instance_metadata=metadata)
    control = app.state.server_control
    boundary = MaintenanceIdentity(str(uuid.uuid4()), "c" * 64)

    def command(operation: str, identity=boundary):
        return control.handler(
            ServerControlRequest(
                request_id=str(uuid.uuid4()),
                instance_id=metadata.instance_id,
                operation=operation,
                selector_id=identity.maintenance_id,
                boundary_sha256=identity.boundary_sha256,
            ),
            ServerControlPeer(pid=os.getpid(), uid=0, gid=0),
        )

    with TestClient(app) as client:
        initial = command("maintenance_status")
        assert not initial.closed and initial.maintenance_id is None
        assert initial.boundary_sha256 is None
        gate = app.state.runtime_admission_gate
        close = gate.close_and_wait

        def close_then_timeout(**kwargs):
            close(**kwargs)
            raise MaintenanceRefused("Timed out after closing HTTP admission.")

        with monkeypatch.context() as patch:
            patch.setattr(gate, "close_and_wait", close_then_timeout)
            with pytest.raises(RuntimeError, match="Timed out"):
                command("maintenance_enter")
        partial = command("maintenance_status")
        assert partial.closed and not partial.quiescent
        assert not app.state.background_admission_gate.closed
        assert client.get("/api/health").status_code == 503
        assert not command("maintenance_release").closed
        assert client.get("/api/health").status_code == 200
        entered = command("maintenance_enter")
        assert entered.closed and entered.quiescent
        assert entered.capture.status == "complete"
        assert Path(entered.capture.receipt_path).is_file()
        assert command("maintenance_enter").capture == entered.capture
        assert client.get("/api/health").status_code == 503
        with pytest.raises(RuntimeError, match="another maintenance boundary"):
            command("maintenance_release", MaintenanceIdentity(str(uuid.uuid4()), "c" * 64))
        assert not command("maintenance_release").closed
        assert client.get("/api/health").status_code == 200
        next_boundary = MaintenanceIdentity(str(uuid.uuid4()), "d" * 64)
        assert (
            command("maintenance_enter", next_boundary).maintenance_id
            == next_boundary.maintenance_id
        )
        command("maintenance_release", next_boundary)


def test_offline_inventory_discovers_roots_without_changing_stopped_state(captured, tmp_path):
    from rcp.server_ops.deployment import OfflinePrepareRequest, offline_inventory

    request, state, _ = captured
    data, research = Path(request.data_dir), Path(state["research"])
    (data / "rcp.lock").unlink(missing_ok=True)
    before = {root: _tree_state(root) for root in (data, research)}
    result = offline_inventory(
        OfflinePrepareRequest(
            version=1,
            data_dir=str(data),
            output_dir=str(tmp_path / "inventory"),
            source_commit="a" * 40,
        )
    )
    assert {root["live"] for root in result["roots"]} == {str(data), str(research)}
    assert {root: _tree_state(root) for root in before} == before


def test_offline_preparation_keeps_live_database_unchanged(captured, tmp_path):
    from rcp.server_ops.deployment import (
        InspectRequest,
        OfflinePrepareRequest,
        inspect,
        offline_prepare,
    )

    request, state, _ = captured
    data = Path(request.data_dir)
    original = (data / "rcp.sqlite3").read_bytes()
    assert inspect(InspectRequest(version=1, data_dir=str(data)))["status"] == "initialized_team"
    result = offline_prepare(
        OfflinePrepareRequest(
            version=1,
            data_dir=str(data),
            output_dir=str(tmp_path / "offline"),
            source_commit="a" * 40,
        )
    )
    assert (data / "rcp.sqlite3").read_bytes() == original
    assert Path(result["migrated_data_root"]).is_dir()
    assert {root["live"] for root in result["roots"]} == {str(data), state["research"]}


def test_offline_protect_uses_complete_typed_capture_and_existing_archive_publisher(
    captured, tmp_path, monkeypatch
):
    from rcp.server_ops.backup import read_backup_archive_receipt
    from rcp.server_ops.deployment import (
        OfflinePrepareRequest,
        OfflineProtectRequest,
        offline_prepare,
        offline_protect,
    )
    from tests.test_backup_encryption import AGE_RECIPIENT, _fake_age

    request, _, _ = captured
    prepared = offline_prepare(
        OfflinePrepareRequest(
            version=1,
            data_dir=request.data_dir,
            output_dir=str(tmp_path / "offline"),
            source_commit="a" * 40,
        )
    )
    fake = _fake_age(tmp_path)
    (tmp_path / "age").symlink_to(fake)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    installation = str(uuid.uuid4())
    output = tmp_path / "protected"
    result = offline_protect(
        OfflineProtectRequest(
            version=1,
            proof_path=prepared["proof_path"],
            proof_sha256=prepared["proof_sha256"],
            output_dir=str(output),
            recipient=AGE_RECIPIENT,
            installation_id=installation,
        )
    )
    assert result["status"] == "protected" and result["uncaptured_projects"] == 0
    receipt = read_backup_archive_receipt(
        Path(result["receipt_path"]),
        expected_destination=output,
        expected_installation_id=installation,
        expected_uid=os.geteuid(),
        verify_digest=True,
    )
    assert receipt.capture_status == "complete"
    assert receipt.protected_project_count == 1


@pytest.mark.parametrize("empty_sqlite", [False, True])
def test_inspect_uninitialized_does_not_create_schema(tmp_path, empty_sqlite):
    from rcp.server_ops.deployment import InspectRequest, inspect

    data = tmp_path / "fresh-data"
    data.mkdir(mode=0o700)
    if empty_sqlite:
        (data / "rcp.sqlite3").write_bytes(b"")
        (data / "rcp.sqlite3").chmod(0o600)
    before = {p.name: p.read_bytes() for p in data.iterdir()}
    assert inspect(InspectRequest(version=1, data_dir=str(data))) == {
        "version": 1,
        "status": "uninitialized",
    }
    assert {p.name: p.read_bytes() for p in data.iterdir()} == before
    (data / "unowned").write_text("do not change")
    assert inspect(InspectRequest(version=1, data_dir=str(data)))["status"] == "uninitialized"
    assert (data / "unowned").read_text() == "do not change"
