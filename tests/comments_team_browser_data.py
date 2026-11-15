"""A team space for the shared-comments browser journey.

`web/tests/commentsTeam.browser.test.mjs` runs `python -m
tests.comments_team_browser_data <empty root>`: Alice and Bob share one project
with an HTML and a PDF artifact from Alice's chat, and Alice's thread on the
HTML artifact mentions Bob. One JSON line names the data and both sign-in tokens.
"""

from __future__ import annotations

import json
import subprocess
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rcp.api import create_app  # noqa: E402
from rcp.storage import AppStore  # noqa: E402
from tests.helpers import signed_in_client  # noqa: E402
from tests.test_artifact_viewer_state import _artifact, _task  # noqa: E402
from tests.test_project_membership import _create_project  # noqa: E402

PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


def _repository(path: Path) -> None:
    path.mkdir(parents=True)
    (path / "README.md").write_text("# Cache study\n", encoding="utf-8")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run([*git, "commit", "-qm", "init"], cwd=path, check=True)


def _enroll(app, code: str, name: str):
    client = signed_in_client(app, base_url="https://team.test")
    token = client.post("/api/team/enroll", json={"code": code, "display_name": name}).json()[
        "token"
    ]
    assert client.post("/api/team/session/exchange", json={"token": token}).status_code == 200
    return client, token


def main(root: Path) -> None:
    root.mkdir(mode=0o700)
    data = root.resolve() / "data"
    _repository(root / "repo")
    store, bootstrap = AppStore.initialize_team_space(data / "rcp.sqlite3", "Comments Lab")
    store.close()
    app = create_app(data_dir=data)
    alice, alice_token = _enroll(app, bootstrap, "Alice")
    bob, bob_token = _enroll(
        app, alice.post("/api/team/invitations", json={}).json()["code"], "Bob"
    )
    bob_id = bob.get("/api/identity").json()["user"]["user_id"]
    project_id = _create_project(alice, root / "repo", name="cache-study")
    invited = alice.post(f"/api/projects/{project_id}/invitations", json={"user_id": bob_id})
    assert invited.status_code == 201, invited.text
    invitation = bob.get("/api/project-invitations").json()[0]["invitation_id"]
    accepted = bob.post(f"/api/project-invitations/{invitation}/accept", json={})
    assert accepted.status_code == 200, accepted.text
    with closing(AppStore(data / "rcp.sqlite3")) as store:
        task = _task(store, project_id)
        page = _artifact(
            store, task, name="figure.html", data=b"<p>Accuracy rose to 78.9 percent.</p>"
        )
        pdf = _artifact(store, task, name="results.pdf", data=PDF)
    created = alice.post(
        f"/api/projects/{project_id}/comment-threads",
        json={
            "target": {"kind": "artifact", "id": page.artifact_id},
            "anchor": {"kind": "whole", "comment": ""},
            "text": f"Check this <@member:{bob_id}>",
        },
    )
    assert created.status_code == 200, created.text
    print(
        json.dumps(
            {
                "data_dir": str(data),
                "alice_token": alice_token,
                "bob_token": bob_token,
                "project_id": project_id,
                "html_id": page.artifact_id,
                "pdf_id": pdf.artifact_id,
                "thread_id": created.json()["thread"]["thread_id"],
            }
        )
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
