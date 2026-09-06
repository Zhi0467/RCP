from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from rcp.transfer import repository_git
from rcp.transfer.repository_git import (
    capture_repository_bundle,
    install_repository_bundle,
    probe_repository_revision,
)
from rcp.transport.ssh import ssh_arguments


def _git(path: Path, *arguments: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "gc.auto=0",
            "-c",
            "maintenance.auto=false",
            "-C",
            str(path),
            *arguments,
        ],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Transfer test",
            "GIT_AUTHOR_EMAIL": "transfer@example.invalid",
            "GIT_COMMITTER_NAME": "Transfer test",
            "GIT_COMMITTER_EMAIL": "transfer@example.invalid",
        },
    ).stdout.strip()


@pytest.fixture
def repositories(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--initial-branch=main", "--template=")
    (source / "code.py").write_text("print('published')\n")
    _git(source, "add", "code.py")
    _git(source, "commit", "-m", "Published code")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "clone", "--bare", str(source), str(origin))
    _git(source, "remote", "add", "origin", str(origin))
    target = tmp_path / "target"
    _git(tmp_path, "clone", str(origin), str(target))
    (source / "code.py").write_text("print('reviewed source')\n")
    _git(source, "commit", "-am", "Unpushed code")
    return source, target, origin


def _capture(source: Path, tmp_path: Path) -> tuple[Path, str]:
    head = probe_repository_revision("", str(source))
    bundle = tmp_path / "source.bundle"
    capture_repository_bundle("", str(source), head, bundle)
    return bundle, head


def test_transfer_installs_unpushed_head_without_changing_source_or_origin(
    repositories: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    source, target, origin = repositories
    published = _git(origin, "rev-parse", "HEAD")
    config = (target / ".git/config").read_bytes()
    (source / "code.py").write_text("staged source edit\n")
    _git(source, "add", "code.py")
    (source / "code.py").write_text("unstaged source edit\n")
    (source / "private.txt").write_text("untracked private input\n")
    index = (source / ".git/index").read_bytes()
    source_branch = _git(source, "symbolic-ref", "HEAD")
    bundle, head = _capture(source, tmp_path)

    install_repository_bundle("", str(target), bundle, head)

    assert (target / "code.py").read_text() == "print('reviewed source')\n"
    assert not (target / "private.txt").exists()
    assert _git(target, "rev-parse", "HEAD") == head
    assert (target / ".git/HEAD").read_text().strip() == head
    assert (target / ".git/config").read_bytes() == config
    assert _git(source, "symbolic-ref", "HEAD") == source_branch
    assert _git(source, "rev-parse", "HEAD") == head
    assert (source / ".git/index").read_bytes() == index
    assert (source / "code.py").read_text() == "unstaged source edit\n"
    assert _git(origin, "rev-parse", "HEAD") == published
    assert _git(target, "rev-parse", "origin/main") == published
    assert _git(source, "bundle", "list-heads", str(bundle)) == f"{head} HEAD"


@pytest.mark.parametrize("change", ["tracked", "staged", "untracked", "ignored", "hidden"])
def test_transfer_refuses_dirty_target_without_overwriting(
    repositories: tuple[Path, Path, Path], tmp_path: Path, change: str
) -> None:
    source, target, _origin = repositories
    bundle, head = _capture(source, tmp_path)
    initial = _git(target, "rev-parse", "HEAD")
    changed = target / ("code.py" if change in {"tracked", "staged", "hidden"} else "notes.txt")
    changed.write_text("target work\n")
    if change == "staged":
        _git(target, "add", "code.py")
    elif change == "hidden":
        _git(target, "update-index", "--assume-unchanged", "code.py")
    elif change == "ignored":
        (target / ".git/info/exclude").write_text("notes.txt\n")
    with pytest.raises(ValueError, match="uncommitted|untracked|hidden"):
        install_repository_bundle("", str(target), bundle, head)
    assert changed.read_text() == "target work\n"
    assert _git(target, "rev-parse", "HEAD") == initial


def test_retry_preserves_owned_research_and_does_not_run_hooks(
    repositories: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    source, target, _origin = repositories
    bundle, head = _capture(source, tmp_path)
    research = target / ".research"
    research.mkdir()
    (research / "patches.jsonl").write_text("human history\n")
    (target / ".git/hooks").mkdir(exist_ok=True)
    hook = target / ".git/hooks/post-checkout"
    marker = tmp_path / "hook-ran"
    hook.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n")
    hook.chmod(0o755)

    install_repository_bundle("", str(target), bundle, head)
    install_repository_bundle("", str(target), bundle, head)

    assert (research / "patches.jsonl").read_text() == "human history\n"
    assert not marker.exists()
    assert _git(target, "rev-parse", "HEAD") == head


def test_retry_after_object_import_completes_checkout(
    repositories: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    source, target, _origin = repositories
    bundle, head = _capture(source, tmp_path)
    _git(target, "bundle", "unbundle", str(bundle))
    assert _git(target, "rev-parse", "HEAD") != head
    install_repository_bundle("", str(target), bundle, head)
    assert _git(target, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("attached", [False, True])
def test_same_head_retry_preserves_published_artifacts_without_git_writes(
    repositories: tuple[Path, Path, Path], tmp_path: Path, attached: bool
) -> None:
    source, target, _origin = repositories
    bundle, head = _capture(source, tmp_path)
    install_repository_bundle("", str(target), bundle, head)
    if attached:
        _git(target, "checkout", "-b", "existing-target-branch")
    (target / "kept-chart.svg").write_text("published artifact bytes\n")
    (target / "ignored-view.html").write_text("published result view bytes\n")
    (target / ".git/info/exclude").write_text("ignored-view.html\n")
    (target / ".git/hooks").mkdir(exist_ok=True)
    hook = target / ".git/hooks/post-checkout"
    marker = tmp_path / "retry-hook-ran"
    hook.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n")
    hook.chmod(0o755)
    before = {
        path.relative_to(target): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in target.rglob("*")
        if path.is_file()
    }

    install_repository_bundle("", str(target), bundle, head)

    after = {
        path.relative_to(target): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in target.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not marker.exists()
    if attached:
        assert _git(target, "symbolic-ref", "HEAD") == "refs/heads/existing-target-branch"


def test_same_head_retry_still_validates_the_incoming_bundle(
    repositories: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    source, target, _origin = repositories
    bundle, head = _capture(source, tmp_path)
    install_repository_bundle("", str(target), bundle, head)
    bundle.write_bytes(b"invalid Git bundle")
    with pytest.raises(ValueError, match="could not bundle"):
        install_repository_bundle("", str(target), bundle, head)


@pytest.mark.parametrize("change", ["tracked", "staged", "hidden"])
def test_same_head_retry_still_refuses_tracked_or_index_changes(
    repositories: tuple[Path, Path, Path], tmp_path: Path, change: str
) -> None:
    source, target, _origin = repositories
    bundle, head = _capture(source, tmp_path)
    install_repository_bundle("", str(target), bundle, head)
    (target / "code.py").write_text("target work after first import\n")
    if change == "staged":
        _git(target, "add", "code.py")
    elif change == "hidden":
        _git(target, "update-index", "--assume-unchanged", "code.py")
    with pytest.raises(ValueError, match="uncommitted|hidden"):
        install_repository_bundle("", str(target), bundle, head)
    assert (target / "code.py").read_text() == "target work after first import\n"
    assert _git(target, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("directory", [".research", ".recovery"])
def test_capture_refuses_git_tracked_rcp_state(
    repositories: tuple[Path, Path, Path], tmp_path: Path, directory: str
) -> None:
    source, _target, _origin = repositories
    (source / directory).mkdir()
    (source / directory / "state.json").write_text("{}")
    _git(source, "add", directory)
    _git(source, "commit", "-m", "Tracked RCP state")
    head = _git(source, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="tracked .research or .recovery"):
        capture_repository_bundle("", str(source), head, tmp_path / "refused.bundle")
    assert not (tmp_path / "refused.bundle").exists()


def test_capture_rejects_source_head_drift_and_existing_destination(
    repositories: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    source, _target, origin = repositories
    old_head = _git(origin, "rev-parse", "HEAD")
    destination = tmp_path / "refused.bundle"
    with pytest.raises(ValueError, match="HEAD changed"):
        capture_repository_bundle("", str(source), old_head, destination)
    assert not destination.exists()
    destination.write_bytes(b"existing bundle")
    with pytest.raises(ValueError):
        capture_repository_bundle("", str(source), _git(source, "rev-parse", "HEAD"), destination)
    assert destination.read_bytes() == b"existing bundle"


@pytest.mark.parametrize("directory", [".research", ".recovery"])
def test_install_refuses_git_tracked_target_rcp_state(
    repositories: tuple[Path, Path, Path], tmp_path: Path, directory: str
) -> None:
    source, target, _origin = repositories
    bundle, head = _capture(source, tmp_path)
    (target / directory).mkdir()
    retained = target / directory / "history.jsonl"
    retained.write_text("existing target history\n")
    _git(target, "add", directory)
    _git(target, "commit", "-m", "Existing canonical history")
    initial = _git(target, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="tracked .research or .recovery"):
        install_repository_bundle("", str(target), bundle, head)
    assert retained.read_text() == "existing target history\n"
    assert _git(target, "rev-parse", "HEAD") == initial


@pytest.mark.parametrize("external_content", ["submodule", "lfs"])
def test_capture_refuses_external_repository_contents(
    repositories: tuple[Path, Path, Path], tmp_path: Path, external_content: str
) -> None:
    source, _target, _origin = repositories
    if external_content == "submodule":
        head = _git(source, "rev-parse", "HEAD")
        _git(source, "update-index", "--add", "--cacheinfo", f"160000,{head},dependency")
    else:
        (source / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n")
        _git(source, "add", ".gitattributes")
    _git(source, "commit", "-m", "External repository contents")
    with pytest.raises(ValueError, match="cannot carry"):
        capture_repository_bundle(
            "", str(source), _git(source, "rev-parse", "HEAD"), tmp_path / "refused.bundle"
        )
    assert not (tmp_path / "refused.bundle").exists()


@pytest.mark.parametrize("corruption", ["bytes", "head", "prerequisite", "extra_ref"])
def test_invalid_bundle_never_changes_target(
    repositories: tuple[Path, Path, Path], tmp_path: Path, corruption: str
) -> None:
    source, target, origin = repositories
    bundle, head = _capture(source, tmp_path)
    initial = _git(target, "rev-parse", "HEAD")
    if corruption == "bytes":
        bundle.write_bytes(b"not a Git bundle")
    elif corruption == "head":
        head = _git(origin, "rev-parse", "HEAD")
    elif corruption == "prerequisite":
        bundle.unlink()
        _git(source, "bundle", "create", str(bundle), f"{initial}..HEAD")
    else:
        bundle.unlink()
        _git(source, "bundle", "create", str(bundle), "HEAD", "main")
    with pytest.raises(ValueError, match="repository Git transfer|repository transfer bundle"):
        install_repository_bundle("", str(target), bundle, head)
    assert _git(target, "rev-parse", "HEAD") == initial
    assert (target / "code.py").read_text() == "print('published')\n"


@pytest.mark.parametrize("unsafe", ["root_symlink", "metadata_symlink", "merge", "filter"])
def test_unsafe_checkout_refused(
    repositories: tuple[Path, Path, Path], tmp_path: Path, unsafe: str
) -> None:
    source, _target, _origin = repositories
    if unsafe == "root_symlink":
        alias = tmp_path / "source-alias"
        alias.symlink_to(source, target_is_directory=True)
        source = alias
    elif unsafe == "metadata_symlink":
        config = source / ".git/config"
        moved = tmp_path / "config"
        config.rename(moved)
        config.symlink_to(moved)
    elif unsafe == "merge":
        (source / ".git/MERGE_HEAD").write_text(_git(source, "rev-parse", "HEAD"))
    else:
        _git(source, "config", "filter.custom.smudge", "false")
    with pytest.raises(ValueError, match="repository transfer"):
        probe_repository_revision("", str(source))


def test_regular_committed_symlink_is_transferred_without_dereference(
    repositories: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    source, target, _origin = repositories
    (source / "link").symlink_to("../outside-secret")
    _git(source, "add", "link")
    _git(source, "commit", "-m", "Ordinary repository symlink")
    bundle, head = _capture(source, tmp_path)
    install_repository_bundle("", str(target), bundle, head)
    assert (target / "link").is_symlink()
    assert os.readlink(target / "link") == "../outside-secret"
    assert not (tmp_path / "outside-secret").exists()


def test_ssh_transport_executes_the_same_shipped_source_with_streamed_bundle(
    repositories: tuple[Path, Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, _origin = repositories
    calls: list[str] = []

    def local_ssh(host: str, command: str) -> list[str]:
        calls.append(host)
        arguments = shlex.split(command)
        assert arguments[:2] == ["python3", "-c"]
        return [sys.executable, *arguments[1:]]

    monkeypatch.setattr(repository_git, "ssh_arguments", local_ssh)
    head = probe_repository_revision("source-host", str(source))
    bundle = tmp_path / "ssh.bundle"
    capture_repository_bundle("source-host", str(source), head, bundle)
    install_repository_bundle("target-host", str(target), bundle, head)
    assert _git(target, "rev-parse", "HEAD") == head
    assert calls == ["source-host", "source-host", "target-host"]


def test_local_transfer_never_uses_frozen_executable_or_loads_shipped_source(
    repositories: tuple[Path, Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, _origin = repositories
    monkeypatch.setattr(sys, "executable", "/not-a-python-interpreter/rcp-backend")

    def forbidden_source() -> str:
        raise AssertionError("local transfer must use the imported implementation")

    monkeypatch.setattr(repository_git, "_remote_source", forbidden_source)
    bundle, head = _capture(source, tmp_path)
    install_repository_bundle("", str(target), bundle, head)
    assert _git(target, "rev-parse", "HEAD") == head
    assert (target / "code.py").read_text() == "print('reviewed source')\n"


def test_frozen_backend_includes_transfer_source() -> None:
    root = Path(__file__).resolve().parents[1]
    specification = (root / "packaging/rcp_backend.spec").read_text()
    hook = (root / "packaging/hooks/validate_frozen_resources.py").read_text()
    assert 'TRANSPORT_ROOT / "remote_transfer_git.py"' in specification
    assert '(str(REMOTE_TRANSFER_GIT), "rcp/transport")' in specification
    assert "_remote_source()" in hook


@pytest.mark.skipif(
    not os.environ.get("RCP_LIVE_TRANSFER_GIT_HOST"),
    reason="set RCP_LIVE_TRANSFER_GIT_HOST for disposable real SSH checkout verification",
)
def test_live_ssh_transfer_round_trip(
    repositories: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """Exercise both SSH directions using only a fresh remote /tmp fixture."""

    host = os.environ["RCP_LIVE_TRANSFER_GIT_HOST"]
    source, target, origin = repositories
    published_head = _git(origin, "rev-parse", "HEAD")
    expected_head = _git(source, "rev-parse", "HEAD")

    def remote(*arguments: str) -> str:
        result = subprocess.run(
            ssh_arguments(host, shlex.join(arguments)),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        return result.stdout.strip()

    fixture_root = remote("mktemp", "-d", "/tmp/rcp-transfer-git-live.XXXXXXXX")
    assert re.fullmatch(r"/tmp/rcp-transfer-git-live\.[A-Za-z0-9]+", fixture_root)
    remote_target = f"{fixture_root}/checkout"
    expected_origin = "https://example.invalid/rcp-transfer-fixture.git"
    try:
        remote("git", "init", "--initial-branch=main", "--template=", remote_target)
        remote(
            "git",
            "-C",
            remote_target,
            "-c",
            "user.name=RCP transfer fixture",
            "-c",
            "user.email=transfer@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "-m",
            "Disposable target baseline",
        )
        remote("git", "-C", remote_target, "remote", "add", "origin", expected_origin)
        assert probe_repository_revision(host, remote_target) != expected_head

        outbound, head = _capture(source, tmp_path)
        install_repository_bundle(host, remote_target, outbound, head)
        install_repository_bundle(host, remote_target, outbound, head)
        assert probe_repository_revision(host, remote_target) == expected_head
        assert remote("git", "-C", remote_target, "config", "--get", "remote.origin.url") == (
            expected_origin
        )
        assert remote("git", "-C", remote_target, "show", "HEAD:code.py") == (
            "print('reviewed source')"
        )

        inbound = tmp_path / "inbound.bundle"
        capture_repository_bundle(host, remote_target, expected_head, inbound)
        install_repository_bundle("", str(target), inbound, expected_head)
        assert _git(target, "rev-parse", "HEAD") == expected_head
        assert (target / "code.py").read_bytes() == (source / "code.py").read_bytes()
        assert _git(target, "remote", "get-url", "origin") == str(origin)
        assert _git(source, "rev-parse", "HEAD") == expected_head
        assert _git(source, "symbolic-ref", "HEAD") == "refs/heads/main"
        assert _git(origin, "rev-parse", "HEAD") == published_head
    finally:
        remote("rm", "-rf", "--", fixture_root)
