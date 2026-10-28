from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext

import pytest

from rcp.paper import PaperFile, PaperLive, PaperOp, PaperSnapshot
from rcp.paper.changes import (
    InvalidChanges,
    OutdatedChange,
    PaperChanges,
    line_changes,
    map_range,
    validate_output,
)
from rcp.storage import AppStore
from rcp.transport import LocalStateWorkspace
from tests.paper_helpers import apply, human_edit, receipts, saved, start_paper


@pytest.fixture
def setup(tmp_path):
    snapshot = PaperSnapshot(
        revision=1,
        files=[
            PaperFile(
                file_id="f1",
                path="section.md",
                kind="text",
                text="".join(f"line {i}\n" for i in range(30)),
            )
        ],
    )
    store = AppStore(tmp_path / "state.sqlite")
    root = tmp_path / "state"
    root.mkdir()
    live = start_paper(PaperLive(store, lambda _: LocalStateWorkspace(root, str(root))))
    apply(
        live, [PaperOp(kind="create", file_id="f1", path="section.md", text=snapshot.files[0].text)]
    )
    snapshot = saved(live)
    changes = PaperChanges(store, live, lambda _: nullcontext())
    output = tmp_path / "changes"
    (output / "files").mkdir(parents=True)
    return snapshot, live, changes, output


@pytest.mark.parametrize(
    "moves",
    [
        {"rename": [["absent", "new"]]},
        {"rename": [["section.md", "section.md"]]},
        {"delete": ["../outside"]},
        {"rename": "wrong"},
        {"delete": ["section.md", "section.md"]},
        {"unknown": []},
    ],
)
def test_invalid_manifest_is_all_or_nothing(setup, moves):
    snapshot, _live, changes, output = setup
    (output / "files/new.md").write_text("valid")
    (output / "moves.json").write_text(json.dumps(moves))
    with pytest.raises(InvalidChanges):
        changes.collect("p", "t", snapshot, output)
    assert changes.groups("p", "t") == []


def test_paths_utf8_symlink_and_size_limits(setup, monkeypatch):
    snapshot, _live, _changes, output = setup
    file = output / "files/section.md"
    file.write_bytes(b"\xff")
    with pytest.raises(InvalidChanges):
        validate_output(output, snapshot)
    file.unlink()
    file.symlink_to(output.parent / "state.sqlite")
    with pytest.raises(InvalidChanges):
        validate_output(output, snapshot)
    file.unlink()
    file.write_text("four")
    monkeypatch.setattr("rcp.paper.changes.PAPER_CHANGES_MAX_FILE_BYTES", 3)
    with pytest.raises(InvalidChanges):
        validate_output(output, snapshot)


def test_delete_write_conflict_and_moves_filename(setup):
    snapshot, _live, _changes, output = setup
    (output / "files/section.md").write_text("replacement")
    (output / "moves.json").write_text('{"delete":["section.md"]}')
    with pytest.raises(InvalidChanges):
        validate_output(output, snapshot)
    (output / "moves.json").unlink()
    (output / "files/moves.json").write_text('{"ordinary":"paper file"}')
    assert "moves.json" in validate_output(output, snapshot)[0]


def test_line_diff_merges_nearby_and_rewrites_large_changes():
    before = "".join(f"line {i}\n" for i in range(100))
    nearby = before.replace("line 3\n", "changed 3\n").replace("line 7\n", "changed 7\n")
    assert len(line_changes(before, nearby)) == 1
    distant = nearby.replace("line 30\n", "changed 30\n")
    assert len(line_changes(before, distant)) == 2
    assert line_changes(before, "rewritten\n")[0][0] == "rewrite"
    many = before
    for index in range(0, 65, 5):
        many = many.replace(f"line {index}\n", f"changed {index}\n")
    assert line_changes(before, many)[0][0] == "rewrite"


def test_identity_offsets_repeated_paragraphs_and_empty_insertions():
    base = "heading\nsame paragraph\nmiddle\nsame paragraph\nend\n"
    offset = base.rindex("same paragraph")
    start, end = map_range(base, "prefix\n" + base, offset, offset + len("same paragraph"))
    assert start == offset + len("prefix\n")
    assert ("prefix\n" + base)[start:end] == "same paragraph"
    insertion = base.index("middle")
    assert map_range(base, "prefix\n" + base, insertion, insertion) == (
        insertion + 7,
        insertion + 7,
    )
    with pytest.raises(OutdatedChange):
        map_range(base, base[:insertion] + "human\n" + base[insertion:], insertion, insertion)
    with pytest.raises(OutdatedChange):
        map_range(base, base[:offset] + "different" + base[offset + 14 :], offset, offset + 14)


def test_accept_idempotency_rebase_and_guarded_undo(setup):
    snapshot, live, changes, output = setup
    before = snapshot.files[0].text
    (output / "files/section.md").write_text(before.replace("line 10\n", "improved 10\n"))
    group = changes.collect("p", "t", snapshot, output)[0]
    human_edit(live, "f1", "prefix\n" + before)
    assert changes.accept("p", group.group_id, "human").state == "accepted"
    assert changes.accept("p", group.group_id, "human").state == "accepted"
    assert len(receipts(live)) == 1
    human_edit(live, "f1", (saved(live).files[0].text or "") + "later\n")
    with pytest.raises(OutdatedChange):
        changes.undo("p", group.group_id, "human")
    assert "improved 10" in saved(live).files[0].text


def test_rename_and_dependent_edits_are_atomic(setup):
    snapshot, live, changes, output = setup
    (output / "moves.json").write_text('{"rename":[["section.md","renamed.md"]]}')
    (output / "files/renamed.md").write_text("new text\n")
    (output / "files/includes.tex").write_text("\\input{renamed.md}\n")
    groups = changes.collect("p", "t", snapshot, output)
    assert len(groups) == 1 and len(groups[0].changes) == 3
    human_edit(live, "f1", "human changed source\n")
    with pytest.raises(OutdatedChange):
        changes.accept("p", groups[0].group_id, "human")
    assert saved(live).files[0].path == "section.md"
    assert not receipts(live)


def test_binary_delete_undo_retains_bytes(setup):
    snapshot, live, changes, output = setup
    body = b"\x89PNG\xff\x00"
    digest = hashlib.sha256(body).hexdigest()
    binary = PaperFile(file_id="b", path="fig.png", kind="binary", blob_sha256=digest)
    apply(
        live,
        [
            PaperOp(
                kind="create",
                file_id=binary.file_id,
                path=binary.path,
                file_kind="binary",
                data=body,
            )
        ],
    )
    snapshot = saved(live)
    (output / "moves.json").write_text('{"delete":["fig.png"]}')
    group = changes.collect("p", "t", snapshot, output)[0]
    changes.accept("p", group.group_id, "human")
    # Undo uses retained bytes after the live file was deleted.
    changes.undo("p", group.group_id, "human")
    changes.undo("p", group.group_id, "human")
    assert live.blob(digest) == body
    assert len(receipts(live)) == 2


def test_direct_mode_admission_and_receipt_recovery(setup, monkeypatch):
    snapshot, live, changes, output = setup
    (output / "files/new.md").write_text("new")
    group = changes.collect("p", "t", snapshot, output, mode="apply_directly")[0]
    state = changes._state
    monkeypatch.setattr(
        changes, "_state", lambda *_args: (_ for _ in ()).throw(RuntimeError("crash after apply"))
    )
    with pytest.raises(RuntimeError):
        changes.accept("p", group.group_id, "Coach, applied directly", directly=True)
    monkeypatch.setattr(changes, "_state", state)
    assert (
        changes.accept("p", group.group_id, "Coach, applied directly", directly=True).state
        == "applied"
    )
    assert len(receipts(live)) == 1

    def deny(_project):
        raise ValueError("admission closed")

    changes.admission = deny
    with pytest.raises(ValueError, match="admission closed"):
        changes.undo("p", group.group_id, "human")


def test_refresh_replaces_only_selected_groups(setup):
    snapshot, live, changes, output = setup
    original = snapshot.files[0].text
    (output / "files/section.md").write_text(original.replace("line 10\n", "coach\n"))
    (output / "files/new.md").write_text("independent\n")
    groups = changes.collect("p", "t", snapshot, output)
    human_edit(live, "f1", original.replace("line 10\n", "human\n"))
    stale = changes.stale_group_ids("p", "t")
    assert len(stale) == 1
    assert changes.stale_file_ids("p", "t") == {"f1"}
    (output / "files/new.md").unlink()
    (output / "files/section.md").write_text(
        saved(live).files[0].text.replace("human\n", "refreshed\n")
    )
    refreshed = changes.collect("p", "t", saved(live), output, replace_group_ids=stale)
    assert len(refreshed) == 2
    assert {g.group_id for g in groups} - stale <= {g.group_id for g in refreshed}


def test_routes_enforce_project_scope_accept_and_undo(manifest, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from tests.helpers import create_named_app, signed_in_client
    from tests.paper_helpers import wire_saved_paper

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    catalog = app.state.catalog
    project_id = app.state.default_project_id
    service = catalog.open(project_id)
    live = wire_saved_paper(service)
    output = tmp_path / "output"
    (output / "files").mkdir(parents=True)
    (output / "files/new.md").write_text("new file")
    group = service.paper_changes.collect(project_id, "task", saved(live, project_id), output)[0]
    base = f"/api/projects/{project_id}/paper/changes"
    from rcp.storage import AgentTaskRecord

    store = service.paper.store
    previous = AgentTaskRecord(
        operation_id="task",
        project_id=project_id,
        kind="paper_coach",
        status="succeeded",
        request={
            "provider": "codex",
            "run_on": "laptop",
            "message": "Revise",
            "mode": "apply_directly",
        },
        native_session_id="native-paper",
        created_at=store.now(),
        updated_at=store.now(),
        status_message="done",
    )
    store.create_agent_task(previous)
    store.record_agent_task_receipt(
        "task", "paper_changes_invalid", {"diagnostics": ["exact failure"]}
    )
    continued = []

    def start(_project, _kind, body, **_kwargs):
        continued.append(body)
        return previous

    monkeypatch.setattr("rcp.api.tasks.start_ordinary_task", start)
    assert TestClient(app).get(base).status_code in {401, 403}
    with signed_in_client(app) as client:
        response = client.get(base, params={"task_id": "task"})
        assert response.status_code == 200
        assert response.json()["groups"][0]["changes"][0]["path"] == "new.md"
        with monkeypatch.context() as patch:

            def closed(_project):
                raise ValueError("maintenance admission closed")

            patch.setattr(service.paper_changes, "admission", closed)
            assert client.get(base).json()["groups"][0]["group_id"] == group.group_id
        assert client.post(f"{base}/{group.group_id}/refresh", json={}).status_code == 200
        assert continued[-1]["session_id"] == "native-paper"
        assert continued[-1]["refresh_group_ids"] == [group.group_id]
        assert continued[-1]["mode"] == "apply_directly"
        assert client.post(f"{base}/retry", json={"task_id": "task"}).status_code == 202
        assert continued[-1]["retry_source_task_id"] == "task"
        assert continued[-1]["refresh_file_ids"] is None
        for _ in range(2):
            response = client.post(f"{base}/{group.group_id}/accept", json={})
            assert response.status_code == 200, response.text
        assert len(receipts(live)) == 1
        assert client.post(f"{base}/{group.group_id}/undo", json={}).status_code == 200
        assert len(receipts(live)) == 2
        assert (
            client.post(
                f"/api/projects/missing/paper/changes/{group.group_id}/accept", json={}
            ).status_code
            == 404
        )


def test_undo_all_reverse_order_and_failed_apply_can_refresh(setup, monkeypatch):
    snapshot, live, changes, output = setup
    before = snapshot.files[0].text
    (output / "files/section.md").write_text(
        before.replace("line 3\n", "first\n").replace("line 20\n", "second\n")
    )
    groups = changes.collect("p", "t", snapshot, output)
    assert len(groups) == 2
    changes.accept_all("p", "t", "human")
    assert all(group.state == "undone" for group in changes.undo_all("p", "t", "human"))
    assert saved(live).files[0].text == before
    (output / "files/section.md").write_text(before.replace("line 10\n", "third\n"))
    group = changes.collect("p", "next", saved(live), output)[0]
    apply = live.apply

    async def refused(*_args, **_kwargs):
        raise OutdatedChange("atomic precondition refusal")

    monkeypatch.setattr(live, "apply", refused)
    with pytest.raises(OutdatedChange):
        changes.accept("p", group.group_id, "human")
    assert changes._receipt("p", f"paper:{group.group_id}:accept") is None
    monkeypatch.setattr(live, "apply", apply)
    refreshed = changes.collect(
        "p",
        "refreshed-task",
        saved(live),
        output,
        replace_group_ids={group.group_id},
        replace_source_task_id="next",
    )
    assert len(refreshed) == 1


def test_binary_replacement_and_new_pdf_use_hash_guards(setup):
    snapshot, live, changes, output = setup
    old, replacement = b"old figure", b"replacement figure"
    digest = hashlib.sha256(old).hexdigest()
    apply(
        live,
        [PaperOp(kind="create", file_id="binary", path="figure.pdf", file_kind="binary", data=old)],
    )
    snapshot = saved(live)
    (output / "files/figure.pdf").write_bytes(replacement)
    (output / "files/new.pdf").write_bytes(b"%PDF ASCII content")
    groups = changes.collect("p", "binary-task", snapshot, output)
    assert {group.changes[0].kind for group in groups} == {"create", "binary"}
    assert all(group.changes[0].file_kind == "binary" for group in groups)
    changes.accept_all("p", "binary-task", "human")
    assert live.blob(hashlib.sha256(replacement).hexdigest()) == replacement
    changes.undo_all("p", "binary-task", "human")
    assert (
        next(file for file in saved(live).files if file.file_id == "binary").blob_sha256 == digest
    )
    assert all(file.path != "new.pdf" for file in saved(live).files)


def test_old_writer_coach_dispatch_migrates_without_rewriting_history(tmp_path):
    from pydantic import ValidationError

    from rcp.storage import AgentTaskRecord

    path = tmp_path / "old-writer.sqlite"
    store = AppStore(path)
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="legacy-coach",
            project_id="project",
            kind="paper_coach",
            status="succeeded",
            request={"provider": "codex"},
            created_at=store.now(),
            updated_at=store.now(),
            status_message="done",
        )
    )
    legacy = {"profile": "ordinary", "task_contract": "paper_readonly", "scope": {}}
    store.record_agent_task_receipt("legacy-coach", "launch", {"capability": "paper_readonly"})
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET dispatch_authority_json=? WHERE operation_id=?",
            (json.dumps(legacy), "legacy-coach"),
        )
        connection.execute(
            "CREATE TABLE paper_apply_receipts (project_id TEXT, receipt_key TEXT, fingerprint TEXT, receipt TEXT)"
        )
        for table in ("paper_change_receipts", "paper_changes", "paper_change_groups"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("DELETE FROM storage_schema_migrations WHERE migration_version>=63")
    with pytest.raises(ValidationError):
        store.agent_task("legacy-coach")
    store.close()
    upgraded = AppStore(path)
    task = upgraded.agent_task("legacy-coach")
    assert task is not None and task.dispatch_authority is not None
    assert task.dispatch_authority.task_contract == "paper_suggest"
    assert task.dispatch_authority.profile == "ordinary"
    assert task.dispatch_authority.scope.run_truth_scope == []
    assert upgraded.agent_task_receipts_by_category("legacy-coach", "launch")[0].payload == {
        "capability": "paper_readonly"
    }
    assert upgraded.storage_schema_ledger_head() == upgraded.storage_schema_registry_head()


@pytest.mark.parametrize("file_cap,total_cap,sizes", [(128, 1024, [129]), (512, 256, [140, 140])])
def test_retained_binary_bases_are_subset_materialized_and_bounded(
    setup, monkeypatch, file_cap, total_cap, sizes
):
    snapshot, live, changes, output = setup
    deletes = []
    for index, size in enumerate(sizes):
        body = bytes([index]) * size
        path = f"figure-{index}.pdf"
        apply(
            live,
            [
                PaperOp(
                    kind="create",
                    file_id=f"binary-{index}",
                    path=path,
                    file_kind="binary",
                    data=body,
                )
            ],
        )
        deletes.append(path)
    unrelated = PaperFile(
        file_id="unrelated", path="unrelated.pdf", kind="binary", blob_sha256="unmaterialized"
    )
    snapshot = saved(live)
    snapshot.files.append(unrelated)
    (output / "moves.json").write_text(json.dumps({"delete": deletes}))
    monkeypatch.setattr("rcp.paper.changes.PAPER_CHANGES_MAX_FILE_BYTES", file_cap)
    monkeypatch.setattr("rcp.paper.changes.PAPER_CHANGES_MAX_TOTAL_BYTES", total_cap)
    materialized = []
    materialize = live.materialize

    def capture(selected, dest):
        materialized.extend(file.path for file in selected.files)
        materialize(selected, dest)

    monkeypatch.setattr(live, "materialize", capture)
    with pytest.raises(InvalidChanges, match="Saved binary base"):
        changes.collect("p", "bounded", snapshot, output)
    assert set(materialized) == set(deletes)
    assert changes.groups("p", "bounded") == []


def test_outstanding_card_keeps_base_past_retention_and_undo_restores_text_identity(
    setup, monkeypatch
):
    from rcp import limits

    snapshot, live, changes, output = setup
    before = snapshot.files[0].text
    (output / "files/section.md").write_text(before.replace("line 10\n", "coach\n"))
    group = changes.collect("p", "t", snapshot, output)[0]
    monkeypatch.setattr(limits, "PAPER_SAVED_REVISION_LIMIT", 1)
    for index in range(3):
        human_edit(live, "f1", before + f"later {index}\n")
    assert live.run_sync(live.text_at("p", "f1", snapshot.revision)) == before
    changes.accept("p", group.group_id, "human")
    assert "coach\n" in saved(live).files[0].text
    changes.undo("p", group.group_id, "human")
    assert saved(live).files[0].text == before + "later 2\n"
    (output / "files/section.md").unlink()
    (output / "moves.json").write_text('{"delete":["section.md"]}')
    from pycrdt import Doc, Text, create_update_message

    cached = Doc({"text": Text()})
    cached.apply_update(live.run_sync(live.connect("p", "f1")).doc.get_update())
    deletion = changes.collect("p", "delete", saved(live), output)[0]
    changes.accept("p", deletion.group_id, "human")
    changes.undo("p", deletion.group_id, "human")
    assert saved(live).files[0].file_id == "f1"
    assert saved(live).files[0].text == before + "later 2\n"
    restored = live.run_sync(live.connect("p", "f1"))
    cached.apply_update(restored.doc.get_update())
    assert str(cached["text"]) == before + "later 2\n"
    live.run_sync(live.receive("p", "f1", create_update_message(cached.get_update()), "human"))
    assert saved(live).files[0].text == before + "later 2\n"


def test_proposed_endpoint_returns_full_text_without_cross_project_access(
    manifest, tmp_path, monkeypatch
):
    from tests.helpers import create_named_app, signed_in_client
    from tests.paper_helpers import wire_saved_paper

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    live = wire_saved_paper(service)
    project = service.paper.project_id
    output = tmp_path / "changes"
    (output / "files").mkdir(parents=True)
    proposed = "<script>untrusted</script>\n" * 100
    (output / "files/paper.md").write_text(proposed)
    group = service.paper_changes.collect(project, "task", saved(live, project), output)[0]
    card = group.changes[0]
    assert card.kind == "rewrite"
    with signed_in_client(app) as client:
        response = client.get(f"/api/projects/{project}/paper/changes/{card.change_id}/proposed")
        assert response.status_code == 200
        assert response.text == proposed
        assert response.headers["content-type"].startswith("text/plain")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["content-disposition"] == "attachment"
        assert (
            client.get(f"/api/projects/missing/paper/changes/{card.change_id}/proposed").status_code
            == 404
        )
        from fastapi import HTTPException

        def revoked(*_args):
            raise HTTPException(status_code=403, detail="Membership revoked")

        monkeypatch.setattr("rcp.api.paper_changes.require_project_membership", revoked)
        assert (
            client.post(
                f"/api/projects/{project}/paper/changes/{group.group_id}/accept", json={}
            ).status_code
            == 403
        )
        assert saved(live, project).files[0].text == "Saved paper text.\n"


def test_active_coach_base_is_retained_before_collection(setup, monkeypatch):
    from rcp import limits
    from rcp.storage import AgentTaskRecord

    snapshot, live, changes, output = setup
    store = live.store
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="active",
            project_id="p",
            kind="paper_coach",
            status="running",
            request={"message": "Review"},
            created_at=store.now(),
            updated_at=store.now(),
            status_message="running",
        )
    )
    store.record_agent_task_receipt("active", "paper_turn_base", {"revision": snapshot.revision})
    monkeypatch.setattr(limits, "PAPER_SAVED_REVISION_LIMIT", 1)
    human_edit(live, "f1", snapshot.files[0].text + "later\n")
    assert live.run_sync(live.text_at("p", "f1", snapshot.revision)) == snapshot.files[0].text
    (output / "files/section.md").write_text(snapshot.files[0].text.replace("line 10\n", "coach\n"))
    assert changes.collect("p", "active", snapshot, output)[0].state == "pending"


def test_new_directory_collision_refuses_without_leaving_a_prepared_receipt(setup):
    snapshot, live, changes, output = setup
    (output / "files/parts").mkdir()
    (output / "files/parts/new.md").write_text("proposal")
    group = changes.collect("p", "t", snapshot, output)[0]
    apply(live, [PaperOp(kind="create", path="parts", text="human")])
    with pytest.raises(OutdatedChange):
        changes.accept("p", group.group_id, "human")
    assert changes._receipt("p", f"paper:{group.group_id}:accept") is None
    assert changes.groups("p", "t")[0].state == "outdated"


@pytest.mark.parametrize("case", ["nul", "path", "doc", "upload", "tree", "count"])
def test_collection_enforces_live_tree_validation(setup, monkeypatch, case):
    snapshot, _live, changes, output = setup
    name, body = ("bad\x01.md", b"ok") if case == "path" else ("new.md", b"a\0b")
    if case in {"doc", "upload", "tree", "count"}:
        name, body = ("new.pdf", b"1234") if case == "upload" else ("new.md", b"1234")
        limit = {
            "doc": "DOC_BYTES",
            "upload": "UPLOAD_BYTES",
            "tree": "TREE_BYTES",
            "count": "FILES",
        }[case]
        monkeypatch.setattr(f"rcp.limits.PAPER_MAX_{limit}", 1)
    (output / "files" / name).write_bytes(body)
    with pytest.raises(InvalidChanges):
        changes.collect("p", "t", snapshot, output)
    assert changes.groups("p", "t") == []


@pytest.mark.parametrize("failure", [ValueError, RuntimeError])
def test_failed_direct_group_releases_receipt_and_continues(setup, monkeypatch, failure):
    snapshot, live, changes, output = setup
    for name in ("a.md", "b.md"):
        (output / "files" / name).write_text("new")
    first, _second = changes.collect("p", "t", snapshot, output, mode="apply_directly")
    original = live.apply

    async def apply_group(project, ops, **kwargs):
        if ops[0].path == "a.md":
            raise failure("apply refused")
        return await original(project, ops, **kwargs)

    monkeypatch.setattr(live, "apply", apply_group)
    assert [g.state for g in changes.accept_all("p", "t", "coach", directly=True)] == [
        "pending",
        "applied",
    ]
    assert changes._receipt("p", f"paper:{first.group_id}:accept") is None
    assert changes.reject("p", first.group_id, "human").state == "rejected"


def test_pure_rename_has_no_line_preview(setup):
    snapshot, _live, changes, output = setup
    (output / "moves.json").write_text('{"rename":[["section.md","renamed.md"]]}')
    card = changes.collect("p", "t", snapshot, output)[0].changes[0]
    assert card.preview == {"removed": [], "added": [], "more": 0}
    assert card.stats == {"added": 0, "removed": 0}


def test_accept_recovers_committed_apply_despite_delivery_failure(setup, monkeypatch):
    snapshot, live, changes, output = setup
    (output / "files/new.md").write_text("new")
    group = changes.collect("p", "t", snapshot, output)[0]
    original = live.apply

    async def apply_then_fail(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("delivery failed after commit")

    monkeypatch.setattr(live, "apply", apply_then_fail)
    assert changes.accept("p", group.group_id, "human").state == "accepted"
    assert "receipt" in changes._receipt("p", f"paper:{group.group_id}:accept")


def test_pending_card_reports_its_anchor_in_live_utf16_offsets(setup):
    snapshot, live, changes, output = setup
    before = snapshot.files[0].text or ""
    (output / "files/section.md").write_text(before.replace("line 10\n", "line 10\nadded\n"))
    group = changes.collect("p", "t", snapshot, output)[0]
    prefix = "😀 edited elsewhere\n"
    human_edit(live, "f1", prefix + before)
    card = changes.groups("p", "t")[0].public()["changes"][0]
    text = saved(live).files[0].text or ""
    offset = text.index("line 11\n")
    expected = len(text[:offset].encode("utf-16-le")) // 2
    assert card["current"] == {"start": expected, "end": expected, "revision": saved(live).revision}
    assert changes.groups("p", "t")[0].group_id == group.group_id


def test_refresh_subset_is_validated_against_the_whole_tree(setup):
    _, live, changes, output = setup
    apply(live, [PaperOp(kind="create", file_id="f2", path="other.md", text="unseen\n")])
    full = saved(live)
    subset = full.model_copy(update={"files": [f for f in full.files if f.file_id == "f1"]})
    (output / "files/other.md").write_text("collides\n")
    with pytest.raises(InvalidChanges):
        changes.collect("p", "t", subset, output)


def test_repeated_deleted_paragraph_makes_card_outdated(setup):
    snapshot, live, changes, output = setup
    base = snapshot.files[0].text.replace("line 3\n", "same paragraph\nsame paragraph\n")
    human_edit(live, "f1", base)
    snapshot = saved(live)
    (output / "files/section.md").write_text(base.replace("same paragraph", "improved", 1))
    group = changes.collect("p", "t", snapshot, output)[0]
    current = base.replace("same paragraph\n", "", 1)
    human_edit(live, "f1", current)
    assert changes.groups("p", "t")[0].state == "outdated"
    with pytest.raises(OutdatedChange):
        changes.accept("p", group.group_id, "human")
    assert saved(live).files[0].text == current


def test_groups_reuses_base_and_mapping_across_cards(setup):
    from unittest.mock import patch

    import rcp.paper.changes as module

    snapshot, live, changes, output = setup
    before = snapshot.files[0].text
    (output / "files/section.md").write_text(
        before.replace("line 3\n", "first\n").replace("line 20\n", "second\n")
    )
    changes.collect("p", "t", snapshot, output)
    with (
        patch.object(live, "text_at", wraps=live.text_at) as reads,
        patch.object(module, "_RangeMapping", wraps=module._RangeMapping) as mappings,
    ):
        assert len(changes.groups("p", "t")) == 2
    assert reads.call_count == mappings.call_count == 1
