from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from pathlib import Path, PurePosixPath

import pytest

import rcp.runs.result_views as result_views
from rcp.runs.result_views import (
    ResultViewSnapshot,
    clear_result_view_rollback_snapshot,
    discover_result_view,
    list_local_result_view_files,
    persist_result_view_rollback_snapshot,
    prepare_local_result_view_slot,
    read_result_view_rollback_snapshot,
    require_result_view_changed,
    restore_result_view,
)
from rcp.transport.run_stage import RemoteRunStage
from rcp.transport.state import StateUnavailable

VIEW_A = "a" * 24
VIEW_B = "b" * 24


def test_local_view_slots_keep_one_conversation_path_across_turns(tmp_path) -> None:
    stage = tmp_path / "rcp-run.chat"
    stage.mkdir()

    first = prepare_local_result_view_slot(stage, VIEW_A, reuse=False)
    first.joinpath("throughput.html").write_text("<h1>linear</h1>", encoding="utf-8")
    old_time = stage.stat().st_mtime - 3600
    os.utime(stage, (old_time, old_time))

    second_turn = prepare_local_result_view_slot(stage, VIEW_A, reuse=True)
    other_view = prepare_local_result_view_slot(stage, VIEW_B, reuse=False)

    assert first == second_turn == stage / "views" / VIEW_A
    assert other_view == stage / "views" / VIEW_B
    assert first != other_view
    assert stage.stat().st_mtime > old_time
    assert not (stage / "turns").exists()
    with pytest.raises(FileExistsError, match="already exists"):
        prepare_local_result_view_slot(stage, VIEW_A, reuse=False)


def test_revision_accepts_atomic_replacement_at_same_path_and_restore_is_conditional(
    tmp_path,
) -> None:
    stage = tmp_path / "rcp-run.chat"
    stage.mkdir()
    slot = prepare_local_result_view_slot(stage, VIEW_A, reuse=False)
    target = slot / "throughput.html"
    target.write_text("<h1>linear</h1>", encoding="utf-8")
    before = discover_result_view(stage, None, VIEW_A)
    old_inode = target.stat().st_ino

    replacement = slot / ".replacement"
    replacement.write_text("<h1>log scale</h1>", encoding="utf-8")
    replacement_inode = replacement.stat().st_ino
    os.replace(replacement, target)
    after = discover_result_view(stage, None, VIEW_A, expected_name=before.name)

    assert target.stat().st_ino == replacement_inode
    assert target.stat().st_ino != old_inode
    require_result_view_changed(before, after)
    assert restore_result_view(stage, None, VIEW_A, before) is True
    restored_inode = target.stat().st_ino
    assert target.read_bytes() == before.data
    assert restore_result_view(stage, None, VIEW_A, before) is False
    assert target.stat().st_ino == restored_inode


@pytest.mark.parametrize("poison", ["rename", "extra", "symlink", "nested"])
def test_local_restore_rebuilds_the_exact_prior_one_file_slot(tmp_path, poison: str) -> None:
    stage = tmp_path / "rcp-run.chat"
    stage.mkdir()
    slot = prepare_local_result_view_slot(stage, VIEW_A, reuse=False)
    target = slot / "throughput.html"
    target.write_text("<h1>original</h1>", encoding="utf-8")
    before = discover_result_view(stage, None, VIEW_A)
    outside = stage / "outside.html"
    outside.write_text("outside stays unchanged", encoding="utf-8")

    if poison == "rename":
        target.rename(slot / "renamed.html")
    elif poison == "extra":
        target.write_text("<h1>changed</h1>", encoding="utf-8")
        slot.joinpath("extra.html").write_text("<h1>extra</h1>", encoding="utf-8")
    elif poison == "symlink":
        target.unlink()
        target.symlink_to(outside)
    else:
        target.unlink()
        target.mkdir()
        target.joinpath("nested.html").write_text("<h1>nested</h1>", encoding="utf-8")

    assert restore_result_view(stage, None, VIEW_A, before) is True
    assert list_local_result_view_files(stage, VIEW_A) == [(before.name, before.size)]
    assert target.read_bytes() == before.data
    assert outside.read_text(encoding="utf-8") == "outside stays unchanged"
    quarantine = _one_result_view_quarantine(stage / "views")
    assert quarantine.parent == stage / "views"
    if poison == "symlink":
        assert quarantine.joinpath(before.name).is_symlink()


@pytest.mark.parametrize("unsafe_kind", ["symlink", "nested"])
def test_local_view_discovery_rejects_non_regular_entries(tmp_path, unsafe_kind: str) -> None:
    stage = tmp_path / "rcp-run.chat"
    stage.mkdir()
    slot = prepare_local_result_view_slot(stage, VIEW_A, reuse=False)
    target = slot / "view.html"
    if unsafe_kind == "symlink":
        outside = stage / "outside.html"
        outside.write_text("<h1>outside</h1>", encoding="utf-8")
        target.symlink_to(outside)
    else:
        target.mkdir()
        target.joinpath("nested.html").write_text("<h1>nested</h1>", encoding="utf-8")

    with pytest.raises(ValueError, match="unsafe entry"):
        list_local_result_view_files(stage, VIEW_A)


def test_local_view_discovery_rejects_oversized_and_non_html_output(tmp_path) -> None:
    stage = tmp_path / "rcp-run.chat"
    stage.mkdir()
    slot = prepare_local_result_view_slot(stage, VIEW_A, reuse=False)
    output = slot / "view.html"
    output.write_bytes(b"<h1>too large</h1>")

    with pytest.raises(ValueError, match="byte limit"):
        discover_result_view(stage, None, VIEW_A, max_bytes=4)

    output.rename(slot / "view.txt")
    with pytest.raises(ValueError, match="descriptively named"):
        discover_result_view(stage, None, VIEW_A)


def test_local_discovery_stops_after_the_second_entry_in_a_wide_slot(tmp_path, monkeypatch) -> None:
    stage = tmp_path / "rcp-run.chat"
    stage.mkdir()
    slot = prepare_local_result_view_slot(stage, VIEW_A, reuse=False)
    for index in range(256):
        slot.joinpath(f"view-{index:03}.html").write_text("<h1>wide</h1>", encoding="utf-8")

    observed = _count_fd_scandir_entries(monkeypatch)

    with pytest.raises(ValueError, match="exactly one"):
        discover_result_view(stage, None, VIEW_A)
    assert observed == [2]


def test_local_restore_checks_only_two_wide_entries_and_quarantines_the_rest(
    tmp_path, monkeypatch
) -> None:
    stage = tmp_path / "rcp-run.chat"
    stage.mkdir()
    slot = prepare_local_result_view_slot(stage, VIEW_A, reuse=False)
    target = slot / "view.html"
    target.write_text("<h1>original</h1>", encoding="utf-8")
    before = discover_result_view(stage, None, VIEW_A)
    for index in range(256):
        slot.joinpath(f"extra-{index:03}.html").write_text("<h1>wide</h1>", encoding="utf-8")

    observed = _count_fd_scandir_entries(monkeypatch)

    assert restore_result_view(stage, None, VIEW_A, before) is True
    assert observed == [2]
    assert target.read_bytes() == before.data
    quarantine = _one_result_view_quarantine(stage / "views")
    assert len(list(quarantine.iterdir())) == 257


def test_local_restore_quarantines_a_deep_tree_without_traversing_its_symlink(
    tmp_path,
) -> None:
    stage = tmp_path / "rcp-run.chat"
    stage.mkdir()
    slot = prepare_local_result_view_slot(stage, VIEW_A, reuse=False)
    target = slot / "view.html"
    target.write_text("<h1>original</h1>", encoding="utf-8")
    before = discover_result_view(stage, None, VIEW_A)
    target.unlink()
    outside = stage / "outside.html"
    outside.write_text("outside stays unchanged", encoding="utf-8")
    descriptors = _make_deep_linked_tree(target, outside, depth=1_050)

    try:
        assert restore_result_view(stage, None, VIEW_A, before) is True
        assert target.read_bytes() == before.data
        quarantine = _one_result_view_quarantine(stage / "views")
        assert quarantine.parent == stage / "views"
        assert stat.S_ISLNK(
            os.stat("external", dir_fd=descriptors[-1], follow_symlinks=False).st_mode
        )
        assert outside.read_text(encoding="utf-8") == "outside stays unchanged"
    finally:
        _remove_open_deep_tree(descriptors, stage / "views", before.name)


def test_local_rollback_snapshot_is_atomic_bounded_and_digest_checked(tmp_path) -> None:
    stage = tmp_path / "rcp-run.chat"
    stage.mkdir()
    slot = prepare_local_result_view_slot(stage, VIEW_A, reuse=False)
    target = slot / "view.html"
    target.write_text("<h1>first</h1>", encoding="utf-8")
    first = discover_result_view(stage, None, VIEW_A)

    persist_result_view_rollback_snapshot(stage, None, VIEW_A, first)
    assert (
        read_result_view_rollback_snapshot(
            stage,
            None,
            VIEW_A,
            expected_name=first.name,
            expected_size=first.size,
            expected_sha256=first.sha256,
        )
        == first
    )

    second_data = b"<h1>second</h1>"
    second = ResultViewSnapshot(
        name=first.name,
        size=len(second_data),
        sha256=hashlib.sha256(second_data).hexdigest(),
        data=second_data,
    )
    persist_result_view_rollback_snapshot(stage, None, VIEW_A, second)
    snapshots = stage / "views" / ".rcp-result-view-snapshots"
    assert [item.name for item in snapshots.iterdir()] == [VIEW_A]
    with pytest.raises(ValueError, match="changed before it could be cleared"):
        clear_result_view_rollback_snapshot(stage, None, VIEW_A, first)
    assert (
        read_result_view_rollback_snapshot(
            stage,
            None,
            VIEW_A,
            expected_name=second.name,
            expected_size=second.size,
            expected_sha256=second.sha256,
        )
        == second
    )
    assert clear_result_view_rollback_snapshot(stage, None, VIEW_A, second) is True
    with pytest.raises(FileNotFoundError, match="snapshot is absent"):
        read_result_view_rollback_snapshot(
            stage,
            None,
            VIEW_A,
            expected_name=second.name,
            expected_size=second.size,
            expected_sha256=second.sha256,
        )


def test_local_first_rollback_snapshot_fsyncs_its_new_parent(tmp_path, monkeypatch) -> None:
    stage = tmp_path / "rcp-run.chat"
    stage.mkdir()
    slot = prepare_local_result_view_slot(stage, VIEW_A, reuse=False)
    slot.joinpath("view.html").write_text("<h1>durable</h1>", encoding="utf-8")
    snapshot = discover_result_view(stage, None, VIEW_A)
    observed: list[tuple[int, int]] = []
    real_fsync = os.fsync

    def recording_fsync(descriptor: int) -> None:
        info = os.fstat(descriptor)
        observed.append((info.st_dev, info.st_ino))
        real_fsync(descriptor)

    monkeypatch.setattr(result_views.os, "fsync", recording_fsync)
    persist_result_view_rollback_snapshot(stage, None, VIEW_A, snapshot)

    views = stage / "views"
    snapshots = views / ".rcp-result-view-snapshots"
    saved = snapshots / VIEW_A
    assert observed == [
        (views.stat().st_dev, views.stat().st_ino),
        (saved.stat().st_dev, saved.stat().st_ino),
        (snapshots.stat().st_dev, snapshots.stat().st_ino),
    ]


def test_local_rollback_snapshot_rejects_symlink_without_touching_target(tmp_path) -> None:
    stage = tmp_path / "rcp-run.chat"
    stage.mkdir()
    slot = prepare_local_result_view_slot(stage, VIEW_A, reuse=False)
    target = slot / "view.html"
    target.write_text("<h1>first</h1>", encoding="utf-8")
    snapshot = discover_result_view(stage, None, VIEW_A)
    persist_result_view_rollback_snapshot(stage, None, VIEW_A, snapshot)
    saved = stage / "views" / ".rcp-result-view-snapshots" / VIEW_A
    saved.unlink()
    outside = stage / "outside.snapshot"
    outside.write_bytes(b"outside stays unchanged")
    saved.symlink_to(outside)

    with pytest.raises(ValueError, match="snapshot target is unsafe"):
        persist_result_view_rollback_snapshot(stage, None, VIEW_A, snapshot)
    with pytest.raises(ValueError, match="snapshot is unsafe"):
        read_result_view_rollback_snapshot(
            stage,
            None,
            VIEW_A,
            expected_name=snapshot.name,
            expected_size=snapshot.size,
            expected_sha256=snapshot.sha256,
        )
    with pytest.raises(ValueError, match="snapshot is unsafe"):
        clear_result_view_rollback_snapshot(stage, None, VIEW_A, snapshot)
    assert saved.is_symlink()
    assert outside.read_bytes() == b"outside stays unchanged"


def test_local_view_slot_rejects_symlinked_components(tmp_path) -> None:
    stage = tmp_path / "rcp-run.chat"
    stage.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (stage / "views").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="parent is unsafe"):
        prepare_local_result_view_slot(stage, VIEW_A, reuse=False)

    linked_stage = tmp_path / "linked-stage"
    linked_stage.symlink_to(stage, target_is_directory=True)
    with pytest.raises(StateUnavailable, match="conversation stage"):
        prepare_local_result_view_slot(linked_stage, VIEW_A, reuse=False)


def test_remote_view_operations_use_exact_stable_slot_and_roll_root_mtime(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "rcp-run.remote"
    (root / "workspace").mkdir(parents=True)
    stage = RemoteRunStage("research.example")
    stage.root = PurePosixPath(str(root))
    _run_remote_scripts_locally(stage, monkeypatch)
    old_time = root.stat().st_mtime - 3600
    os.utime(root, (old_time, old_time))

    slot = Path(str(stage.prepare_result_view_slot(VIEW_A, reuse=False)))
    payload = b"<html><body>first</body></html>"
    slot.joinpath("throughput.html").write_bytes(payload)

    assert stage.prepare_result_view_slot(VIEW_A, reuse=True) == PurePosixPath(str(slot))
    assert stage.prepare_result_view_slot(VIEW_B, reuse=False) != PurePosixPath(str(slot))
    assert stage.list_result_view_files(VIEW_A) == [("throughput.html", len(payload))]
    assert stage.read_result_view_bytes(VIEW_A, "throughput.html", max_bytes=1024) == payload
    assert (
        stage.restore_result_view_bytes(VIEW_A, "throughput.html", payload, max_bytes=1024) is False
    )
    replacement = b"<html><body>restored</body></html>"
    assert (
        stage.restore_result_view_bytes(VIEW_A, "throughput.html", replacement, max_bytes=1024)
        is True
    )
    assert slot.joinpath("throughput.html").read_bytes() == replacement
    assert root.stat().st_mtime > old_time
    assert not (root / "workspace" / "turns").exists()


def test_remote_view_operations_distinguish_missing_unsafe_and_unavailable(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "rcp-run.remote"
    (root / "workspace").mkdir(parents=True)
    stage = RemoteRunStage("research.example")
    stage.root = PurePosixPath(str(root))
    _run_remote_scripts_locally(stage, monkeypatch)

    with pytest.raises(FileNotFoundError, match="slot is absent"):
        stage.list_result_view_files(VIEW_A)
    slot = Path(str(stage.prepare_result_view_slot(VIEW_A, reuse=False)))
    with pytest.raises(FileNotFoundError, match="file is absent"):
        stage.read_result_view_bytes(VIEW_A, "missing.html", max_bytes=1024)
    outside = root / "outside.html"
    outside.write_text("<h1>outside</h1>", encoding="utf-8")
    slot.joinpath("linked.html").symlink_to(outside)
    with pytest.raises(ValueError, match="unsafe entry"):
        stage.list_result_view_files(VIEW_A)

    monkeypatch.setattr(
        stage,
        "_ssh",
        lambda _arguments: subprocess.CompletedProcess([], 255, "", "connection lost"),
    )
    with pytest.raises(StateUnavailable, match="connection lost"):
        stage.list_result_view_files(VIEW_A)


def test_remote_view_listing_returns_after_two_wide_entries(tmp_path, monkeypatch) -> None:
    root = tmp_path / "rcp-run.remote"
    (root / "workspace").mkdir(parents=True)
    stage = RemoteRunStage("research.example")
    stage.root = PurePosixPath(str(root))
    _run_remote_scripts_locally(stage, monkeypatch)
    slot = Path(str(stage.prepare_result_view_slot(VIEW_A, reuse=False)))
    for index in range(256):
        slot.joinpath(f"view-{index:03}.html").write_text("<h1>wide</h1>", encoding="utf-8")

    scripts: list[str] = []
    run_remote = stage._ssh

    def capture(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        if len(arguments) >= 3 and arguments[:2] == ["python3", "-c"]:
            scripts.append(arguments[2])
        return run_remote(arguments)

    monkeypatch.setattr(stage, "_ssh", capture)

    assert len(stage.list_result_view_files(VIEW_A)) == 2
    assert len(scripts) == 1
    assert "os.listdir(slot_fd)" not in scripts[0]
    assert "with os.scandir(slot_fd)" in scripts[0]
    assert "if len(result)==2: break" in scripts[0]


def test_remote_restore_rebuilds_a_slot_with_renamed_linked_and_nested_output(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "rcp-run.remote"
    (root / "workspace").mkdir(parents=True)
    stage = RemoteRunStage("research.example")
    stage.root = PurePosixPath(str(root))
    _run_remote_scripts_locally(stage, monkeypatch)
    slot = Path(str(stage.prepare_result_view_slot(VIEW_A, reuse=False)))
    original = b"<html><body>original</body></html>"
    target = slot / "throughput.html"
    target.write_bytes(original)
    target.rename(slot / "renamed.html")
    outside = root / "outside.html"
    outside.write_text("outside stays unchanged", encoding="utf-8")
    slot.joinpath("linked.html").symlink_to(outside)
    nested = slot / "nested"
    nested.mkdir()
    nested.joinpath("extra.html").write_text("<h1>extra</h1>", encoding="utf-8")

    assert (
        stage.restore_result_view_bytes(
            VIEW_A,
            "throughput.html",
            original,
            max_bytes=1024,
        )
        is True
    )
    assert stage.list_result_view_files(VIEW_A) == [("throughput.html", len(original))]
    assert target.read_bytes() == original
    assert outside.read_text(encoding="utf-8") == "outside stays unchanged"
    quarantine = _one_result_view_quarantine(root / "workspace" / "views")
    assert quarantine.parent == root / "workspace" / "views"
    assert quarantine.joinpath("linked.html").is_symlink()


def test_remote_restore_quarantines_a_deep_tree_without_traversing_its_symlink(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "rcp-run.remote"
    (root / "workspace").mkdir(parents=True)
    stage = RemoteRunStage("research.example")
    stage.root = PurePosixPath(str(root))
    _run_remote_scripts_locally(stage, monkeypatch)
    slot = Path(str(stage.prepare_result_view_slot(VIEW_A, reuse=False)))
    target = slot / "view.html"
    original = b"<html><body>original</body></html>"
    target.write_bytes(original)
    target.unlink()
    outside = root / "outside.html"
    outside.write_text("outside stays unchanged", encoding="utf-8")
    descriptors = _make_deep_linked_tree(target, outside, depth=1_050)

    scripts: list[str] = []
    run_remote = stage._ssh_bytes

    def capture(
        arguments: list[str], *, input_data: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        if len(arguments) >= 3 and arguments[:2] == ["python3", "-c"]:
            scripts.append(arguments[2])
        return run_remote(arguments, input_data=input_data)

    monkeypatch.setattr(stage, "_ssh_bytes", capture)

    try:
        assert (
            stage.restore_result_view_bytes(
                VIEW_A,
                "view.html",
                original,
                max_bytes=1024,
            )
            is True
        )
        assert target.read_bytes() == original
        quarantine = _one_result_view_quarantine(root / "workspace" / "views")
        assert quarantine.parent == root / "workspace" / "views"
        assert stat.S_ISLNK(
            os.stat("external", dir_fd=descriptors[-1], follow_symlinks=False).st_mode
        )
        assert outside.read_text(encoding="utf-8") == "outside stays unchanged"
        assert len(scripts) == 1
        assert "def remove_entry" not in scripts[0]
        assert "os.listdir(slot)" not in scripts[0]
        assert "with os.scandir(slot)" in scripts[0]
    finally:
        _remove_open_deep_tree(
            descriptors,
            root / "workspace" / "views",
            "view.html",
        )


def test_remote_rollback_snapshot_is_bounded_digest_checked_and_nofollow(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "rcp-run.remote"
    (root / "workspace").mkdir(parents=True)
    stage = RemoteRunStage("research.example")
    stage.root = PurePosixPath(str(root))
    _run_remote_scripts_locally(stage, monkeypatch)
    slot = Path(str(stage.prepare_result_view_slot(VIEW_A, reuse=False)))
    target = slot / "view.html"
    target.write_text("<h1>remote</h1>", encoding="utf-8")
    snapshot = discover_result_view(None, stage, VIEW_A)
    scripts: list[str] = []
    remote_bytes = stage._ssh_bytes

    def capture(arguments: list[str], *, input_data: bytes | None = None):
        scripts.append(arguments[2])
        return remote_bytes(arguments, input_data=input_data)

    monkeypatch.setattr(stage, "_ssh_bytes", capture)

    persist_result_view_rollback_snapshot(None, stage, VIEW_A, snapshot)
    assert "os.fsync(views_fd)" in scripts[0]
    assert (
        read_result_view_rollback_snapshot(
            None,
            stage,
            VIEW_A,
            expected_name=snapshot.name,
            expected_size=snapshot.size,
            expected_sha256=snapshot.sha256,
        )
        == snapshot
    )
    saved = root / "workspace" / "views" / ".rcp-result-view-snapshots" / VIEW_A
    saved.unlink()
    outside = root / "outside.snapshot"
    outside.write_bytes(b"outside stays unchanged")
    saved.symlink_to(outside)

    with pytest.raises(ValueError, match="snapshot is unsafe"):
        persist_result_view_rollback_snapshot(None, stage, VIEW_A, snapshot)
    with pytest.raises(ValueError, match="snapshot is unsafe"):
        read_result_view_rollback_snapshot(
            None,
            stage,
            VIEW_A,
            expected_name=snapshot.name,
            expected_size=snapshot.size,
            expected_sha256=snapshot.sha256,
        )
    with pytest.raises(ValueError, match="snapshot is unsafe"):
        clear_result_view_rollback_snapshot(None, stage, VIEW_A, snapshot)
    assert saved.is_symlink()
    assert outside.read_bytes() == b"outside stays unchanged"


def test_remote_view_traversal_rejects_replaced_workspace_or_views(tmp_path, monkeypatch) -> None:
    root = tmp_path / "rcp-run.remote"
    outside = tmp_path / "outside"
    outside.mkdir()
    root.mkdir()
    (root / "workspace").symlink_to(outside, target_is_directory=True)
    stage = RemoteRunStage("research.example")
    stage.root = PurePosixPath(str(root))
    _run_remote_scripts_locally(stage, monkeypatch)

    with pytest.raises(StateUnavailable, match="workspace"):
        stage.prepare_result_view_slot(VIEW_A, reuse=False)

    (root / "workspace").unlink()
    (root / "workspace").mkdir()
    (root / "workspace" / "views").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="slot is unsafe"):
        stage.prepare_result_view_slot(VIEW_A, reuse=False)


def test_view_id_is_exactly_lowercase_24_hex(tmp_path) -> None:
    stage = tmp_path / "rcp-run.chat"
    stage.mkdir()

    for value in ("a" * 23, "A" * 24, "g" * 24, "../" + "a" * 24):
        with pytest.raises(ValueError, match="24 lowercase hexadecimal"):
            prepare_local_result_view_slot(stage, value, reuse=False)


def _run_remote_scripts_locally(stage: RemoteRunStage, monkeypatch) -> None:
    monkeypatch.setattr(
        stage,
        "_ssh",
        lambda arguments: subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            check=False,
        ),
    )
    monkeypatch.setattr(
        stage,
        "_ssh_bytes",
        lambda arguments, *, input_data=None: subprocess.run(
            arguments,
            capture_output=True,
            input=input_data,
            check=False,
        ),
    )


def _one_result_view_quarantine(views: Path) -> Path:
    entries = list(views.iterdir())
    assert sum(item.name == VIEW_A for item in entries) == 1
    quarantines = [item for item in entries if item.name.startswith(".rcp-result-view-old-")]
    assert len(quarantines) == 1
    assert len(entries) == 2
    return quarantines[0]


def _count_fd_scandir_entries(monkeypatch) -> list[int]:
    original_scandir = os.scandir
    observed = [0]

    class CountingIterator:
        def __init__(self, iterator) -> None:
            self._iterator = iterator

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            self._iterator.close()

        def __iter__(self):
            return self

        def __next__(self):
            entry = next(self._iterator)
            observed[0] += 1
            return entry

    def counted(path):
        iterator = original_scandir(path)
        return CountingIterator(iterator) if isinstance(path, int) else iterator

    monkeypatch.setattr(result_views.os, "scandir", counted)
    return observed


def _make_deep_linked_tree(target: Path, outside: Path, *, depth: int) -> list[int]:
    target.mkdir()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptors = [os.open(target, flags)]
    try:
        for _ in range(depth):
            os.mkdir("d", mode=0o700, dir_fd=descriptors[-1])
            descriptors.append(os.open("d", flags, dir_fd=descriptors[-1]))
        os.symlink(str(outside), "external", dir_fd=descriptors[-1])
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise
    return descriptors


def _remove_open_deep_tree(descriptors: list[int], views: Path, public_name: str) -> None:
    """Iteratively remove the test tree through its already-open, nofollow descriptors."""
    root_inode = os.fstat(descriptors[0]).st_ino
    os.unlink("external", dir_fd=descriptors[-1])
    for index in range(len(descriptors) - 1, 0, -1):
        os.close(descriptors[index])
        os.rmdir("d", dir_fd=descriptors[index - 1])
    os.close(descriptors[0])

    candidates = [views / VIEW_A / public_name]
    quarantines = [
        item for item in views.iterdir() if item.name.startswith(".rcp-result-view-old-")
    ]
    candidates.extend(item / public_name for item in quarantines)
    for candidate in candidates:
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(info.st_mode) and info.st_ino == root_inode:
            candidate.rmdir()
    for quarantine in quarantines:
        if not any(quarantine.iterdir()):
            quarantine.rmdir()
