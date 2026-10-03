from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from rcp.limits import LESSON_TEXT_MAX_CHARS, LESSONS_PER_PROJECT_MAX
from rcp.storage import ProjectRecord
from rcp.storage.lessons import LessonError, lesson_edit_authorized

from .test_work_questions import work_execution


@pytest.fixture
def store(tmp_path):
    execution, human = work_execution(tmp_path)
    store = execution.store
    owner = store.local_owner
    store.upsert_project(
        ProjectRecord(
            project_id="project",
            locator="local",
            name="Project",
            state_location="local",
            state_remote=False,
            added_at=store.now(),
        )
    )
    store.seat_project_member("project", owner.user_id)
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET authorized_user_id=? WHERE operation_id='turn'", (owner.user_id,)
        )
    return store


def consolidate(store):
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO consolidation_runs(run_id,project_id,occurrence_date,operation_id,"
            "authorization_id,authorized_by_json,input_head,created_at) "
            "VALUES ('run','project','2026-10-03','turn','authorization','{}',0,'now')"
        )


def human_edit(store, lesson_id):
    return store.update_lesson(
        "project", lesson_id, "human", user_id=store.local_owner.user_id, display_name="Human"
    )


def command(store, subcommand, **kwargs):
    return store.execute_lesson_command("project", "turn", subcommand, **kwargs)


def test_lesson_limits_and_human_ownership(store):
    owner = store.local_owner
    with pytest.raises(LessonError, check=lambda exc: exc.code == "lesson_text_invalid"):
        store.add_lesson(
            "project",
            "x" * (LESSON_TEXT_MAX_CHARS + 1),
            user_id=owner.user_id,
            display_name="Human",
        )
    for i in range(LESSONS_PER_PROJECT_MAX):
        store.add_lesson("project", str(i), user_id=owner.user_id, display_name="Human")
    assert all(item["human_owned"] for item in store.list_lessons("project"))
    with pytest.raises(LessonError, check=lambda exc: exc.code == "lessons_limit"):
        command(store, "add", key="full", text="overflow")


def test_keyed_receipts_survive_delete_and_reject_changed_arguments(store):
    consolidate(store)
    first = command(store, "add", key="add", text="same")
    assert command(store, "add", key="add", text="same") == first
    with pytest.raises(LessonError, check=lambda exc: exc.code == "lesson_key_conflict"):
        command(store, "add", key="add", text="different")
    lesson_id = first["lesson"]["lesson_id"]
    assert command(store, "delete", key="del", lesson_id=lesson_id) == {}
    assert command(store, "delete", key="del", lesson_id=lesson_id) == {}
    assert command(store, "add", key="add", text="same") == first
    assert store.list_lessons("project") == []


def test_edit_authority_and_live_membership(store):
    lesson = command(store, "add", key="a", text="tip")["lesson"]
    assert not lesson_edit_authorized(store, "turn")
    for verb in ("update", "delete", "list"):
        with pytest.raises(LessonError, check=lambda exc: exc.code == "lesson_forbidden"):
            command(store, verb, key=verb, lesson_id=lesson["lesson_id"], text="new")
    consolidate(store)
    assert lesson_edit_authorized(store, "turn")
    assert not lesson_edit_authorized(store, "other")
    with store.connection() as connection:
        connection.execute("DELETE FROM project_members WHERE project_id='project'")
    with pytest.raises(LessonError, check=lambda exc: exc.code == "lesson_forbidden"):
        command(store, "add", key="a", text="tip")


@pytest.mark.parametrize("verb", ["update", "delete"])
def test_human_edit_and_agent_mutation_are_serialized(store, verb):
    consolidate(store)
    lesson_id = command(store, "add", key="a", text="tip")["lesson"]["lesson_id"]

    def human():
        try:
            human_edit(store, lesson_id)
            return True
        except LessonError as exc:
            assert verb == "delete" and exc.code == "lesson_not_found"
            return False

    def agent():
        try:
            command(store, verb, key="race", lesson_id=lesson_id, text="agent")
        except LessonError as exc:
            assert exc.code == "lesson_human_owned"

    with ThreadPoolExecutor(max_workers=2) as pool:
        human_result = pool.submit(human)
        agent_result = pool.submit(agent)
        survived = human_result.result()
        agent_result.result()
    if survived:
        lesson = store.list_lessons("project")[0]
        assert lesson["text"] == "human" and lesson["human_owned"]
        with pytest.raises(LessonError, check=lambda exc: exc.code == "lesson_human_owned"):
            command(store, verb, key="after", lesson_id=lesson_id, text="agent")


def test_consolidation_can_page_and_prune_beyond_render_cutoff(store):
    from rcp.limits import LESSONS_RENDER_MAX_BYTES
    from rcp.runs.lessons import render_lessons

    consolidate(store)
    lessons = [
        command(store, "add", key=str(index), text="x" * LESSON_TEXT_MAX_CHARS)["lesson"]
        for index in range(80)
    ]
    rendered = render_lessons(store.list_lessons("project"))
    assert len(rendered.encode()) <= LESSONS_RENDER_MAX_BYTES
    hidden = lessons[0]["lesson_id"]
    assert hidden not in rendered
    cursor = None
    listed = []
    while True:
        page = command(store, "list", cursor=cursor)
        listed.extend(page["lessons"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert {lesson["lesson_id"] for lesson in listed} == {lesson["lesson_id"] for lesson in lessons}
    assert all(not lesson["human_owned"] for lesson in listed)
    command(store, "delete", key="prune-hidden", lesson_id=hidden)
    assert hidden not in {lesson["lesson_id"] for lesson in store.list_lessons("project")}


def test_lessons_and_receipts_survive_restore_and_alias_then_delete(store):
    from uuid import uuid4

    from rcp.transfer import TRANSFER_EXCLUDED_PROJECT_TABLES

    original = command(store, "add", key="a", text="retained")["lesson"]
    tables = {"operational_lessons", "lesson_command_receipts"}
    assert tables <= TRANSFER_EXCLUDED_PROJECT_TABLES
    store.detach_restored_lifecycle(diagnostic="Restore test", confirmed_by="Human")
    assert store.list_lessons("project") == [original]
    canonical = str(uuid4())
    store.migrate_project_identity("project", canonical, str(uuid4()))
    assert store.list_lessons(canonical) == [original]
    with store.connection() as connection:
        for table in tables:
            assert connection.execute(f"SELECT project_id FROM {table}").fetchone()[0] == canonical
    counts = store.delete_project_records(canonical)
    for table in tables:
        assert counts[table] == 1
        with store.connection() as connection:
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
