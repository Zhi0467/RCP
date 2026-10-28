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
    _RangeMapping,
    line_changes,
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
    assert line_changes(before, "rewritten\n")[0].kind == "rewrite"
    many = before
    for index in range(0, 65, 5):
        many = many.replace(f"line {index}\n", f"changed {index}\n")
    assert line_changes(before, many)[0].kind == "rewrite"


def test_identity_offsets_repeated_paragraphs_and_empty_insertions():
    base = "heading\nsame paragraph\nmiddle\nsame paragraph\nend\n"
    offset = base.rindex("same paragraph")
    start, end = _RangeMapping(base, "prefix\n" + base).map(offset, offset + len("same paragraph"))
    assert start == offset + len("prefix\n")
    assert ("prefix\n" + base)[start:end] == "same paragraph"
    insertion = base.index("middle")
    assert _RangeMapping(base, "prefix\n" + base).map(insertion, insertion) == (
        insertion + 7,
        insertion + 7,
    )
    with pytest.raises(OutdatedChange):
        _RangeMapping(base, base[:insertion] + "human\n" + base[insertion:]).map(
            insertion, insertion
        )
    with pytest.raises(OutdatedChange):
        _RangeMapping(base, base[:offset] + "different" + base[offset + 14 :]).map(
            offset, offset + 14
        )


def test_accept_idempotency_rebase_and_guarded_undo(setup):
    snapshot, live, changes, output = setup
    before = snapshot.files[0].text
    (output / "files/section.md").write_text(before.replace("line 10\n", "improved 10\n"))
    group = changes.collect("p", "t", snapshot, output)[0]
    human_edit(live, "f1", "prefix\n" + before)
    assert changes.accept("p", group.group_id, "human").state == "accepted"
    assert changes.accept("p", group.group_id, "human").state == "accepted"
    assert len(receipts(live)) == 1
    accepted = saved(live).files[0].text or ""
    # An edit inside the accepted region refuses Undo; one elsewhere does not.
    human_edit(live, "f1", accepted.replace("improved 10", "improved 10 more"))
    with pytest.raises(OutdatedChange):
        changes.undo("p", group.group_id, "human")
    assert "improved 10 more" in saved(live).files[0].text
    human_edit(live, "f1", accepted + "later\n")
    assert changes.undo("p", group.group_id, "human").state == "undone"
    assert saved(live).files[0].text == "prefix\n" + before + "later\n"


def test_undo_never_deletes_a_created_file_the_human_typed_in(setup):
    snapshot, live, changes, output = setup
    (output / "files/new.md").write_text("new\n")
    group = changes.collect("p", "t", snapshot, output)[0]
    changes.accept("p", group.group_id, "human")
    created = next(file for file in saved(live).files if file.path == "new.md")
    human_edit(live, created.file_id, "new\nmine\n")
    with pytest.raises(OutdatedChange):
        changes.undo("p", group.group_id, "human")
    assert any(file.text == "new\nmine\n" for file in saved(live).files)


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

    class Closed(Exception):
        pass

    def deny(_project):
        raise Closed

    changes.admission = deny
    with pytest.raises(Closed):
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
        with monkeypatch.context() as patch:

            def no_group_scan(*_args):
                raise AssertionError("Refresh must load only its selected group")

            patch.setattr(PaperChanges, "groups", no_group_scan)
            assert client.post(f"{base}/{group.group_id}/refresh", json={}).status_code == 200
            assert client.post(f"{base}/missing/refresh", json={}).status_code == 404
        assert continued[-1]["session_id"] == "native-paper"
        assert continued[-1]["refresh_group_ids"] == [group.group_id]
        assert continued[-1]["mode"] == "apply_directly"
        assert client.post(f"{base}/{group.group_id}/accept", json={}).status_code == 200
        assert client.post(f"{base}/{group.group_id}/refresh", json={}).status_code == 409
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


def test_old_writer_coach_dispatch_migrates_without_rewriting_history(tmp_path):
    from pydantic import ValidationError

    from rcp.storage import AgentTaskRecord
    from tests.test_paper_live_storage import PAPER_TABLES

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
        for table in PAPER_TABLES:
            connection.execute(f'DROP TABLE "{table}"')
        connection.execute("DELETE FROM storage_schema_migrations WHERE migration_version=63")
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


@pytest.mark.parametrize("state", ["pending", "outdated"])
@pytest.mark.parametrize("kind", ["binary", "create", "delete"])
def test_binary_cards_survive_collection_and_undo(setup, monkeypatch, state, kind):
    from rcp.paper.persistence import collect_bodies

    _, live, changes, output = setup
    old, new = b"original figure", b"new figure"
    if kind != "create":
        apply(
            live,
            [
                PaperOp(
                    kind="create", file_id="figure", path="figure.pdf", file_kind="binary", data=old
                )
            ],
        )
    snapshot = saved(live)
    if kind == "delete":
        (output / "moves.json").write_text('{"delete":["figure.pdf"]}')
    else:
        (output / "files/figure.pdf").write_bytes(new)

    def no_materialization(*_args):
        pytest.fail("Binary originals must not be extracted to temporary files")

    monkeypatch.setattr(live, "materialize", no_materialization)
    group = changes.collect("p", "binary", snapshot, output)[0]
    changes._state("p", group.group_id, state)
    with live.store.connection() as connection:
        payload = connection.execute("SELECT payload FROM paper_changes").fetchone()[0]
        assert "before_bytes" not in payload and "after_bytes" not in payload
        collect_bodies(connection)
    assert changes.accept("p", group.group_id, "human").state == "accepted"
    if kind != "delete":
        assert live.blob(hashlib.sha256(new).hexdigest()) == new
    with live.store.connection() as connection:
        collect_bodies(connection)
        assert all(
            "bytes_base64" not in row[0]
            for row in connection.execute("SELECT payload FROM paper_change_receipts")
        )
    assert changes.undo("p", group.group_id, "human").state == "undone"
    assert changes.undo("p", group.group_id, "human").state == "undone"
    assert len(receipts(live)) == 2
    figures = [file for file in saved(live).files if file.path == "figure.pdf"]
    if kind == "create":
        assert figures == []
    else:
        assert figures[0].blob_sha256 == hashlib.sha256(old).hexdigest()
        assert live.blob(figures[0].blob_sha256) == old


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


def test_new_directory_collision_marks_card_outdated(setup):
    snapshot, live, changes, output = setup
    (output / "files/parts").mkdir()
    (output / "files/parts/new.md").write_text("proposal")
    group = changes.collect("p", "t", snapshot, output)[0]
    apply(live, [PaperOp(kind="create", path="parts", text="human")])
    with pytest.raises(OutdatedChange):
        changes.accept("p", group.group_id, "human")
    assert changes.groups("p", "t")[0].state == "outdated"


@pytest.mark.parametrize("case", ["nul", "path", "doc", "upload", "tree", "count"])
def test_collection_enforces_live_tree_validation(setup, monkeypatch, case):
    snapshot, _live, changes, output = setup
    # NUL is refused in an existing text file; a new file with NUL is binary.
    name, body = ("bad\x01.md", b"ok") if case == "path" else ("section.md", b"a\0b")
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


def test_failed_direct_group_releases_receipt_and_continues(setup, monkeypatch):
    snapshot, live, changes, output = setup
    for name in ("a.md", "b.md"):
        (output / "files" / name).write_text("new")
    first, _second = changes.collect("p", "t", snapshot, output, mode="apply_directly")
    original = live.apply

    async def apply_group(project, ops, **kwargs):
        if ops[0].path == "a.md":
            raise RuntimeError("apply refused")
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
    original_line = group.public()["changes"][0]["current"]["line"]
    prefix = "😀 edited elsewhere\nsecond inserted line\n"
    human_edit(live, "f1", prefix + before)
    card = changes.groups("p", "t")[0].public()["changes"][0]
    text = saved(live).files[0].text or ""
    offset = text.index("line 11\n")
    expected = len(text[:offset].encode("utf-16-le")) // 2
    assert card["current"] == {
        "line": original_line + 2,
        "start": expected,
        "end": expected,
        "revision": saved(live).revision,
        "text": "",
    }
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


@pytest.mark.parametrize("state", [None, "accepted"])
def test_groups_without_pending_cards_need_no_live_content(setup, monkeypatch, state):
    snapshot, _live, changes, output = setup
    if state is not None:
        (output / "files/new.md").write_text("new")
        group = changes.collect("p", "task", snapshot, output)[0]
        changes._state("p", group.group_id, state)

    def unavailable(*_args):
        raise RuntimeError("Live content is unavailable")

    monkeypatch.setattr(changes, "_snapshot", unavailable)
    assert [group.state for group in changes.groups("p", "task")] == (
        [] if state is None else [state]
    )


def test_an_added_final_newline_is_not_a_card(setup):
    snapshot, live, changes, output = setup
    text = snapshot.files[0].text.rstrip("\n")
    human_edit(live, "f1", text)
    (output / "files/section.md").write_text(text.replace("line 10\n", "coach 10\n") + "\n")
    (group,) = changes.collect("p", "t", saved(live), output)
    assert [change.after for change in group.changes] == ["coach 10\n"]


def test_typing_during_accept_never_outdates_a_valid_card(setup):
    snapshot, live, changes, output = setup
    before = snapshot.files[0].text
    apply(live, [PaperOp(kind="create", file_id="f2", path="other.md", text="other\n")])
    (output / "files/section.md").write_text(before.replace("line 10\n", "coach 10\n"))
    group = changes.collect("p", "t", snapshot, output)[0]
    prepare = changes._prepare
    typed: list[tuple[str, str]] = []

    def prepare_then_type(*args, **kwargs):
        result = prepare(*args, **kwargs)
        if typed:
            human_edit(live, *typed.pop())
        return result

    changes._prepare = prepare_then_type
    try:
        # Another file changed: the card's file is intact, so the accept applies.
        typed.append(("f2", "typed\n"))
        assert changes.accept("p", group.group_id, "human").state == "accepted"
        changes.undo("p", group.group_id, "human")
        retry = changes.collect("p", "t2", saved(live), output)[0]
        # The same file changed outside the card: refused once, still reviewable.
        typed.append(("f1", before.replace("line 25\n", "human 25\n")))
        with pytest.raises(OutdatedChange):
            changes.accept("p", retry.group_id, "human")
    finally:
        changes._prepare = prepare
    assert changes.groups("p", "t2")[0].state == "pending"
    assert changes.accept("p", retry.group_id, "human").state == "accepted"


def test_a_crash_after_the_live_apply_still_shows_the_decision(setup, monkeypatch):
    snapshot, live, changes, output = setup
    before = snapshot.files[0].text
    (output / "files/section.md").write_text(before.replace("line 10\n", "coach 10\n"))
    group = changes.collect("p", "t", snapshot, output, mode="apply_directly")[0]

    def killed(*_args, **_kwargs):
        raise SystemExit("killed after the live commit")

    monkeypatch.setattr(changes, "_state", killed)
    with pytest.raises(SystemExit):
        changes.accept("p", group.group_id, "coach", directly=True)
    monkeypatch.undo()
    assert changes.groups("p", "t")[0].state == "applied"
    changes.undo("p", group.group_id, "human")
    assert saved(live).files[0].text == before
