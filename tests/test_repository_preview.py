from __future__ import annotations

import inspect
import json
import shlex
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest
from fastapi.testclient import TestClient

import rcp.repository_preview as preview_module
import rcp.repository_window as window_module
from rcp.repository_preview import (
    RepositorySource,
    load_repository_source,
    load_repository_source_for_path,
    repository_source_document,
)
from rcp.transport import StateUnavailable
from rcp.transport.ssh import SSH_OPTIONS

from .helpers import create_named_app as create_app


def test_local_repository_source_is_bounded_utf8_and_does_not_follow_symlinks(
    manifest,
) -> None:
    root = Path(manifest.repository_map["repo-a"].path)
    nested = root / "src"
    nested.mkdir()
    (nested / "safe.py").write_text("print('safe')\n", encoding="utf-8")

    source = load_repository_source(manifest, "repo-a", "src/safe.py", max_bytes=64)

    assert source.text == "print('safe')\n"
    assert source.complete
    assert source.start_line == 1
    bounded = load_repository_source(manifest, "repo-a", "src/safe.py", max_bytes=4)
    assert bounded.text == "prin"
    assert not bounded.complete
    assert bounded.total_bytes == 14
    (nested / "binary.dat").write_bytes(b"\xff")
    with pytest.raises(ValueError, match="UTF-8"):
        load_repository_source(manifest, "repo-a", "src/binary.dat")
    for name, content in (("nul.txt", b"safe\x00unsafe"), ("escape.txt", b"safe\x1bunsafe")):
        (nested / name).write_bytes(content)
        with pytest.raises(ValueError, match="control"):
            load_repository_source(manifest, "repo-a", f"src/{name}")
    (nested / "ordinary-controls.txt").write_bytes(b"tab\tok\r\n")
    assert (
        load_repository_source(manifest, "repo-a", "src/ordinary-controls.txt").text
        == "tab\tok\r\n"
    )
    with pytest.raises(ValueError, match="bounded"):
        load_repository_source(manifest, "repo-a", "src")
    (nested / "linked.py").symlink_to(nested / "safe.py")
    with pytest.raises(ValueError, match="safely"):
        load_repository_source(manifest, "repo-a", "src/linked.py")
    (root / "linked-src").symlink_to(nested, target_is_directory=True)
    with pytest.raises(ValueError, match="regular file|safely"):
        load_repository_source(manifest, "repo-a", "linked-src/safe.py")


@pytest.mark.parametrize(
    "path",
    ["", ".", "..", "/etc/passwd", "src/../secret", "src/./safe.py", "src//safe.py"],
)
def test_repository_source_rejects_unsafe_paths(manifest, path: str) -> None:
    with pytest.raises(ValueError, match="relative|unsafe"):
        load_repository_source(manifest, "repo-a", path)


def test_absolute_repository_path_resolves_one_segment_boundary_match(manifest) -> None:
    root = Path(manifest.repository_map["repo-a"].path)
    target = root / "src" / "safe.py"
    target.parent.mkdir()
    target.write_text("safe", encoding="utf-8")

    source = load_repository_source_for_path(manifest, target.as_posix())

    assert source.repository_alias == "repo-a"
    assert source.relative_path == "src/safe.py"
    assert source.text == "safe"
    with pytest.raises(ValueError, match="outside every"):
        load_repository_source_for_path(manifest, "/outside/configured/repositories.py")
    with pytest.raises(ValueError, match="outside every"):
        load_repository_source_for_path(manifest, f"{root}-sibling/file.py")


@pytest.mark.parametrize("nested", [False, True], ids=["equal-roots", "nested-roots"])
def test_absolute_repository_path_refuses_ambiguous_roots_before_reading(
    manifest,
    monkeypatch,
    nested: bool,
) -> None:
    root = PurePosixPath(manifest.repository_map["repo-a"].path)
    manifest.repository_map["repo-b"].path = str(root / "nested" if nested else root)
    target = root / "nested" / "answer.py"

    def unexpected_reader(*_args, **_kwargs):
        raise AssertionError("ambiguous paths must not reach a repository reader")

    monkeypatch.setattr(preview_module, "_read_local_file", unexpected_reader)
    monkeypatch.setattr(preview_module, "_read_remote_file", unexpected_reader)

    with pytest.raises(ValueError, match="repo-a, repo-b"):
        load_repository_source_for_path(manifest, target.as_posix())


def test_repository_source_document_escapes_content_and_highlights_requested_line() -> None:
    source = RepositorySource(
        repository_alias='repo"><script>alert(1)</script>',
        relative_path="src/<unsafe>.py",
        text='first\n<script>alert("source")</script>',
    )

    document = repository_source_document(source, line=2).decode("utf-8")

    assert "<script>" not in document
    assert "&lt;script&gt;alert(&quot;source&quot;)&lt;/script&gt;" in document
    assert 'id="L1" class="line"' in document
    assert 'id="L2" class="line selected"' in document
    with pytest.raises(ValueError, match="outside"):
        repository_source_document(source, line=3)


def _numbered_lines(count: int) -> str:
    return "".join(f"line {number:04d}\n" for number in range(1, count + 1))


def test_oversized_repository_file_returns_the_window_around_the_cited_line(manifest) -> None:
    root = Path(manifest.repository_map["repo-a"].path)
    (root / "trajectory.jsonl").write_text(_numbered_lines(500), encoding="utf-8")

    source = load_repository_source(
        manifest,
        "repo-a",
        "trajectory.jsonl",
        line=250,
        max_bytes=3000,
    )

    assert not source.complete
    assert source.total_bytes == 5000
    assert source.start_line == 250 - preview_module.REPOSITORY_PREVIEW_WINDOW_LINES
    lines = source.text.split("\n")
    assert lines[0] == "line 0150"
    assert lines[-1] == "line 0350"

    tight = load_repository_source(manifest, "repo-a", "trajectory.jsonl", line=250, max_bytes=300)
    assert tight.start_line == 250
    assert tight.text.split("\n")[0] == "line 0250"
    assert len(tight.text) <= 300

    without_line = load_repository_source(manifest, "repo-a", "trajectory.jsonl", max_bytes=3000)
    assert without_line.start_line == 1
    assert without_line.text.split("\n")[0] == "line 0001"
    assert len(without_line.text.split("\n")) == 2 * preview_module.REPOSITORY_PREVIEW_WINDOW_LINES


def test_oversized_lines_stay_bounded_and_keep_whole_characters(manifest) -> None:
    root = Path(manifest.repository_map["repo-a"].path)
    (root / "one-line.log").write_text("x" * 5000, encoding="utf-8")
    (root / "multibyte.log").write_text(("é" * 1000 + "\n") * 3, encoding="utf-8")

    undelimited = load_repository_source(manifest, "repo-a", "one-line.log", max_bytes=300)
    assert undelimited.text == "x" * 300
    assert not undelimited.complete

    # An odd bound cuts the last two-byte character in half.
    multibyte = load_repository_source(manifest, "repo-a", "multibyte.log", max_bytes=1001)
    assert multibyte.text == "é" * 500

    # A byte that is invalid rather than merely cut short still fails.
    (root / "binary.log").write_bytes(b"text\xff" * 200)
    with pytest.raises(ValueError, match="UTF-8"):
        load_repository_source(manifest, "repo-a", "binary.log", max_bytes=100)


def test_a_cited_line_that_fills_the_budget_stops_the_scan(manifest, monkeypatch) -> None:
    root = Path(manifest.repository_map["repo-a"].path)
    (root / "one-line.log").write_bytes(b"x" * 20_000_000)
    reads = 0
    real_read = window_module.os.read

    def counted_read(fd: int, size: int) -> bytes:
        nonlocal reads
        reads += 1
        return real_read(fd, size)

    monkeypatch.setattr(window_module.os, "read", counted_read)
    source = load_repository_source(manifest, "repo-a", "one-line.log", max_bytes=1024)

    assert source.text == "x" * 1024
    # Without the stop, filling a 1 KiB budget would read all twenty chunks.
    assert reads <= 2


def test_window_document_numbers_real_lines_and_names_the_whole_file() -> None:
    source = RepositorySource(
        repository_alias="repo-a",
        relative_path="trajectory.jsonl",
        text="line 0150\nline 0151",
        start_line=150,
        complete=False,
        total_bytes=581_681_685,
    )

    document = repository_source_document(source, line=151).decode("utf-8")

    assert 'id="L150" class="line"' in document
    assert 'id="L151" class="line selected"' in document
    assert "lines 150–151 of a 581,681,685-byte file" in document
    for outside in (149, 152):
        with pytest.raises(ValueError, match="outside"):
            repository_source_document(source, line=outside)


def test_the_shipped_reader_source_windows_the_same_way_as_the_local_call(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "trajectory.jsonl").write_text(_numbered_lines(500), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            inspect.getsource(window_module),
            str(root),
            "trajectory.jsonl",
            "3000",
            "250",
            str(preview_module.REPOSITORY_PREVIEW_WINDOW_LINES),
        ],
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    header, _separator, payload = result.stdout.partition(b"\n")
    assert json.loads(header) == {"start_line": 150, "complete": False, "total_bytes": 5000}
    lines = payload.decode("utf-8").split("\n")
    assert lines[0] == "line 0150"
    assert lines[-1] == "line 0350"

    tight = subprocess.run(
        [
            sys.executable,
            "-c",
            inspect.getsource(window_module),
            str(root),
            "trajectory.jsonl",
            "300",
            "250",
            str(preview_module.REPOSITORY_PREVIEW_WINDOW_LINES),
        ],
        capture_output=True,
        check=False,
    )

    assert tight.returncode == 0, tight.stderr
    tight_header, _tight_separator, tight_payload = tight.stdout.partition(b"\n")
    assert json.loads(tight_header)["start_line"] == 250
    assert tight_payload.split(b"\n")[0] == b"line 0250"
    assert len(tight_payload) <= 300


def test_repository_preview_route_windows_an_oversized_file(
    manifest, tmp_path, monkeypatch
) -> None:
    source_path = Path(manifest.repository_map["repo-b"].path) / "trajectory.jsonl"
    oversized = preview_module.REPOSITORY_PREVIEW_MAX_BYTES // 10 + 1000
    source_path.write_text(_numbered_lines(oversized), encoding="utf-8")
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    client = TestClient(app)

    response = client.get(
        f"/api/projects/{app.state.default_project_id}/repositories/files/preview",
        params={"path": str(source_path), "line": 250},
    )

    assert response.status_code == 200
    assert 'id="L250" class="line selected"' in response.text
    assert 'id="L150" class="line"' in response.text
    assert "line 0149" not in response.text

    # HEAD is the client's preflight, so it must agree with GET about this exact
    # window: a line past the end of the file has to fail both, not only the GET.
    url = f"/api/projects/{app.state.default_project_id}/repositories/files/preview"
    head = client.head(url, params={"path": str(source_path), "line": 250})

    assert head.status_code == 200
    assert head.content == b""
    beyond = {"path": str(source_path), "line": oversized + 1_000}
    assert client.head(url, params=beyond).status_code == 422
    assert client.get(url, params=beyond).status_code == 422


def test_remote_repository_source_uses_multiplexed_ssh_reader(
    manifest,
    monkeypatch,
) -> None:
    manifest.machine_map["laptop"].host = "research@example.test"
    captured: dict[str, object] = {}

    def run(arguments, **kwargs):
        captured["arguments"] = arguments
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            arguments,
            0,
            b'{"start_line": 1, "complete": true, "total_bytes": 12}\nremote text\n',
            b"",
        )

    monkeypatch.setattr(preview_module.subprocess, "run", run)

    source = load_repository_source_for_path(
        manifest,
        f"{manifest.repository_map['repo-a'].path}/nested/file.py",
        max_bytes=123,
    )

    assert source.text == "remote text\n"
    assert source.complete
    assert source.total_bytes == 12
    arguments = captured["arguments"]
    assert isinstance(arguments, list)
    assert arguments[: 1 + len(SSH_OPTIONS)] == ["ssh", *SSH_OPTIONS]
    assert arguments[-2] == "research@example.test"
    remote_arguments = shlex.split(arguments[-1])
    assert remote_arguments[:2] == ["python3", "-c"]
    assert remote_arguments[-5:] == [
        manifest.repository_map["repo-a"].path,
        "nested/file.py",
        "123",
        "0",
        str(preview_module.REPOSITORY_PREVIEW_WINDOW_LINES),
    ]
    assert captured["kwargs"] == {
        "capture_output": True,
        "timeout": preview_module.REPOSITORY_PREVIEW_TIMEOUT_SECONDS,
        "check": False,
    }


@pytest.mark.parametrize(
    ("returncode", "error"),
    [
        (44, FileNotFoundError),
        (45, ValueError),
        (255, StateUnavailable),
    ],
)
def test_remote_repository_source_maps_reader_failures(
    manifest,
    monkeypatch,
    returncode: int,
    error: type[Exception],
) -> None:
    manifest.machine_map["laptop"].host = "research@example.test"

    def run(arguments, **_kwargs):
        return subprocess.CompletedProcess(arguments, returncode, b"", b"unavailable")

    monkeypatch.setattr(preview_module.subprocess, "run", run)

    with pytest.raises(error):
        load_repository_source(manifest, "repo-a", "nested/file.py")


def test_repository_preview_route_returns_escaped_get_and_empty_head(manifest, tmp_path) -> None:
    source_path = Path(manifest.repository_map["repo-b"].path) / "answer.py"
    source_path.write_text("first\n<script>alert(1)</script>\n", encoding="utf-8")
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    client = TestClient(app)
    url = f"/api/projects/{project_id}/repositories/files/preview"

    response = client.get(url, params={"path": str(source_path), "line": 2})

    assert response.status_code == 200
    assert "<script>" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert 'id="L2" class="line selected"' in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"].startswith("sandbox;")

    head = client.head(url, params={"path": str(source_path), "line": 2})
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["cache-control"] == "no-store"


def test_repository_preview_route_maps_missing_and_invalid_requests(manifest, tmp_path) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    client = TestClient(app)
    url = f"/api/projects/{project_id}/repositories/files/preview"
    repo_a = Path(manifest.repository_map["repo-a"].path)

    assert client.get(url, params={"path": "/outside/repositories/answer.py"}).status_code == 422
    assert client.get(url, params={"path": str(repo_a / "missing.py")}).status_code == 404
    assert client.get(url, params={"path": f"{repo_a}/../secret"}).status_code == 422
    assert (
        client.get(
            url,
            params={"path": str(repo_a / "missing.py"), "line": 0},
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/projects/not-registered/repositories/files/preview",
            params={"path": str(repo_a / "answer.py")},
        ).status_code
        == 404
    )


def test_repository_preview_route_names_ambiguous_aliases_before_reading(
    manifest,
    tmp_path,
    monkeypatch,
) -> None:
    root = Path(manifest.repository_map["repo-a"].path)
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    manifest_text = manifest.path.read_text(encoding="utf-8")
    repo_b_root = manifest.repository_map["repo-b"].path
    manifest.path.write_text(
        manifest_text.replace(
            f'path = "{repo_b_root}"',
            f'path = "{root}"',
        ),
        encoding="utf-8",
    )

    def unexpected_reader(*_args, **_kwargs):
        raise AssertionError("ambiguous paths must not reach a repository reader")

    monkeypatch.setattr(preview_module, "_read_local_file", unexpected_reader)
    monkeypatch.setattr(preview_module, "_read_remote_file", unexpected_reader)

    response = TestClient(app).get(
        f"/api/projects/{project_id}/repositories/files/preview",
        params={"path": str(root / "answer.py")},
    )

    assert response.status_code == 422
    assert response.json()["detail"].endswith("repo-a, repo-b")


def test_repository_preview_route_reloads_the_registered_manifest(manifest, tmp_path) -> None:
    original_root = Path(manifest.repository_map["repo-b"].path)
    replacement_root = tmp_path / "replacement-repo"
    replacement_root.mkdir()
    (original_root / "answer.py").write_text("stale", encoding="utf-8")
    (replacement_root / "answer.py").write_text("live", encoding="utf-8")
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    manifest_text = manifest.path.read_text(encoding="utf-8")
    manifest.path.write_text(
        manifest_text.replace(
            f'path = "{original_root}"',
            f'path = "{replacement_root}"',
        ),
        encoding="utf-8",
    )

    response = TestClient(app).get(
        f"/api/projects/{project_id}/repositories/files/preview",
        params={"path": str(replacement_root / "answer.py")},
    )

    assert response.status_code == 200
    assert ">live</span>" in response.text
    assert ">stale</span>" not in response.text
