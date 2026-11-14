from __future__ import annotations

from rcp.paper import PaperOp
from rcp.runs.paper_turns import end_paper_turn
from tests.helpers import create_named_app, signed_in_client
from tests.lifecycles.agent_task import admitted_launch_task, work_request


def test_a_turn_lists_the_agents_files_clashes_first_and_serves_each_diff(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project = app.state.default_project_id
    store = app.state.background_tasks.store
    live = app.state.paper_live
    folder = tmp_path / "data/paper" / project
    with signed_in_client(app) as client:
        prefix = f"/api/projects/{project}/paper"
        for path, text in (("a.md", "one\ntwo\nthree\n"), ("b.md", "b\n"), ("c.md", "c\n")):
            op = PaperOp(kind="write", path=path, data=text.encode(), exclusive=True)
            live.run_sync(live.apply(project, [op], "human"))
        admitted_launch_task(
            store,
            operation_id="turn",
            request=work_request("coach").model_copy(update={"focus": "paper"}),
            record_updates={"project_id": project},
        )
        live.run_sync(live.begin_turn(project, "turn"))
        for path, text in (("a.md", "one\nTWO human\nthree\n"), ("c.md", "c human\n")):
            op = PaperOp(kind="write", path=path, data=text.encode())
            live.run_sync(live.apply(project, [op], "human"))
        (folder / "b.md").write_text("b\nagent\n")
        (folder / "a.md").write_text("one\ntwo agent\nthree\n")
        live.run_sync(live.poll(project, full=True))
        end_paper_turn(store, live, project, "turn")

        turn = client.get(prefix + "/turns/turn")
        assert turn.status_code == 200, turn.text
        rows = turn.json()["files"]
        assert [(row["path"], row["clash"]) for row in rows] == [("a.md", True), ("b.md", False)]
        assert (rows[1]["added"], rows[1]["removed"]) == (1, 0)

        clash = client.get(prefix + "/turns/turn/files/a.md").json()
        assert clash["coach"] == "one\ntwo agent\nthree\n"
        assert clash["current"] == "one\nTWO human\nthree\n"
        (hunk,) = clash["hunks"]
        assert (hunk["old_start"], hunk["old"], hunk["new"]) == (2, ["two"], ["two agent"])

        added = client.get(prefix + "/turns/turn/files/b.md").json()
        assert added["current"] is None
        assert [(h["new_start"], h["new"]) for h in added["hunks"]] == [(2, ["agent"])]

        assert client.get(prefix + "/turns/turn/files/c.md").status_code == 404
        assert client.get(prefix + "/turns/missing").status_code == 404
