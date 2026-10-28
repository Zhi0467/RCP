"""Paper fixtures written by the interpreter being qualified, never current SQL."""

from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from pathlib import Path

ANCESTOR = "# Introduction\n\nCommon ancestor.\n"
DRAFT = "# Introduction\n\nUnpublished human draft.\n"
CANONICAL = "# Introduction\n\nIndependent published edit.\n"
FIGURE = b"\x89PNG\r\n\x1a\nold-figure\x00"
EDITED_FIGURE = b"\x89PNG\r\n\x1a\nnew-figure\x00"


def seed_legacy_paper(data_dir: Path, fixture: dict) -> dict:
    """Execute the historical Paper writer so draft, base and canonical diverge."""
    from rcp.config import load_manifest
    from rcp.paper import PaperService
    from rcp.storage import AppStore

    store = AppStore(data_dir / "rcp.sqlite3")
    record = store.project(fixture["project_id"])
    assert record is not None
    paper = PaperService(load_manifest(Path(record.locator)), store, project_id=record.project_id)

    def save(text, snapshot):
        arguments = {"content": text, "base_hash": snapshot.base_hash}
        if "draft_version" in inspect.signature(paper.save).parameters:
            arguments["draft_version"] = snapshot.draft_version
        return paper.save(**arguments)

    saved = save(ANCESTOR, paper.create())
    # This is a second canonical writer, leaving the first writer's draft base.
    with paper.workspace.transaction():
        paper._write_canonical(CANONICAL)
    behind = save(DRAFT, saved)
    assert behind.sync_state == "behind"
    assert behind.content == DRAFT and behind.incoming_content == CANONICAL
    with store.connection() as connection:
        row = connection.execute(
            "SELECT * FROM paper_drafts WHERE project_id = ?", (record.project_id,)
        ).fetchone()
        assert row["base_hash"] == hashlib.sha256(ANCESTOR.encode()).hexdigest()
        ancestor = "ancestor_content" in dict(row)
        if ancestor:
            assert row["ancestor_content"] == ANCESTOR
    close = getattr(store, "close", None)
    if close is not None:
        close()
    return {"writer": "legacy", "ancestor_stored": ancestor, "draft_sha256": _sha(DRAFT.encode())}


def seed_current_paper(request, fixture: dict, cookie: str) -> dict:
    """Exercise the live REST writer while divergent migration holds publication."""
    base = f"/api/projects/{fixture['project_id']}/paper"

    def call(path, *, body=None, method="GET", content_type="application/json"):
        with request(
            base + path, body=body, method=method, cookie=cookie, content_type=content_type
        ) as response:
            return json.load(response)

    def upload(path, content, *, endpoint="/uploads", method="POST"):
        boundary = uuid.uuid4().hex
        payload = (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="path"\r\n\r\n'
                f'{path}\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{Path(path).name}"\r\nContent-Type: application/octet-stream\r\n\r\n'
            ).encode()
            + content
            + f"\r\n--{boundary}--\r\n".encode()
        )
        return call(
            endpoint,
            body=payload,
            method=method,
            content_type=f"multipart/form-data; boundary={boundary}",
        )

    tree = call("/tree")
    assert tree["hold"] is not None, "Divergent legacy paper must retain its publication hold"
    history = call("/history")
    preserved = set()
    for version in history:
        files = version["manifest"]
        preserved.update(file.get("text_sha256") for file in files)
    # The draft becomes the live text; the diverged canonical and ancestor become versions.
    assert [file["sha256"] for file in tree["files"]] == [_sha(DRAFT.encode())]
    required = {_sha(CANONICAL.encode())}
    if fixture["paper"]["ancestor_stored"]:
        required.add(_sha(ANCESTOR.encode()))
    assert required <= preserved, "Migration lost a divergent legacy history version"
    for path in ("sections/methods.tex", "references.bib"):
        call("/files", body={"path": path, "kind": "text"}, method="POST")
    upload("figures/result.png", FIGURE)
    before = call("/tree")
    ids = {file["path"]: file["file_id"] for file in before["files"]}
    version = call("/history", body={"name": "Before rename"}, method="POST")
    call(
        f"/files/{ids['sections/methods.tex']}/rename",
        body={"path": "sections/results.tex"},
        method="POST",
    )
    upload(
        "figures/result.png",
        EDITED_FIGURE,
        endpoint=f"/files/{ids['figures/result.png']}/content",
        method="PUT",
    )
    saved = call("/tree")
    assert saved["saved_revision"] > before["saved_revision"]
    assert saved["saved_revision"] > saved["published_revision"]
    assert saved["hold"] == tree["hold"]
    assert (
        next(file for file in saved["files"] if file["file_id"] == ids["sections/methods.tex"])[
            "path"
        ]
        == "sections/results.tex"
    )
    result = {
        "writer": "current",
        "saved_revision": saved["saved_revision"],
        "version_id": version["version_id"],
        "file_ids": ids,
        "figure_sha256": _sha(EDITED_FIGURE),
        "hold": saved["hold"],
    }
    return result


def assert_current_paper(request, fixture: dict, cookie: str, expected: dict) -> None:
    base = f"/api/projects/{fixture['project_id']}/paper"
    with request(base + "/tree", cookie=cookie) as response:
        tree = json.load(response)
    assert tree["saved_revision"] == expected["saved_revision"]
    assert tree["hold"] == expected["hold"]
    figure = expected["file_ids"]["figures/result.png"]
    with request(f"{base}/files/{figure}/raw", cookie=cookie) as response:
        assert _sha(response.read()) == expected["figure_sha256"]
    with request(f"{base}/history", cookie=cookie) as response:
        manifest = next(
            row for row in json.load(response) if row["version_id"] == expected["version_id"]
        )
    files = manifest["manifest"]
    assert any(file["path"] == "sections/methods.tex" for file in files)
    assert any(file.get("blob_sha256") == _sha(FIGURE) for file in files)


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
