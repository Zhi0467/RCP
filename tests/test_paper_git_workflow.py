from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

import pytest

from rcp.paper import PaperLive, git_sync
from rcp.paper.folder import LocalPaperFolder
from rcp.paper.git_sync import GitEntry, PaperGit, _git, build_commit, read_tree
from rcp.storage import AppStore
from tests.paper_helpers import human_edit, saved, start_paper


@pytest.fixture
def sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    remote = tmp_path / "remote"
    _git(remote, ["init", "--bare", "--template=", str(remote / "repo.git")])
    monkeypatch.setattr("rcp.paper.git_sync.remote_url", lambda _: str(remote / "repo.git"))
    root = tmp_path / "paper"
    root.mkdir()
    store = AppStore(tmp_path / "state.sqlite")
    live = start_paper(PaperLive(store, lambda _: LocalPaperFolder(root)))
    manager = PaperGit(store, live, tmp_path, lambda _: nullcontext())
    return manager, live, remote


def commit_remote(remote: Path, tree: dict[str, GitEntry], parent: str = "") -> str:
    commit = build_commit(remote, tree, parent, "remote edit", "Coauthor", "coauthor")
    _git(remote, ["update-ref", "refs/heads/main", commit])
    return commit


def test_a_pull_merges_clean_edits_and_github_wins_an_overlap(sync):
    manager, live, remote = sync
    base = "".join(f"line {i}\n" for i in range(50))
    first = commit_remote(remote, {"paper.md": GitEntry(base.encode())})
    manager.key("p", "owner/repo", "main", "member")
    manager.link("p")
    file = saved(live).files[0]
    clean = base.replace("line 40\n", "clean\n")
    second = commit_remote(remote, {"paper.md": GitEntry(clean.encode())}, first)
    manager.pull("p")
    assert saved(live).files[0].text == clean
    human_edit(
        live, file.file_id, clean.replace("line 10\n", "local\n").replace("line 2\n", "mine\n")
    )
    overlap = clean.replace("line 10\n", "remote\n")
    commit_remote(remote, {"paper.md": GitEntry(overlap.encode())}, second)
    manager.pull("p")
    assert saved(live).files[0].text == overlap.replace("line 2\n", "mine\n")
    assert "conflicts" not in manager.status("p")


def test_push_after_an_overlap_reads_member_commit_from_remote(sync):
    manager, live, remote = sync
    test_a_pull_merges_clean_edits_and_github_wins_an_overlap(sync)
    commit = manager.push("p", "message", "Member", "member")
    assert read_tree(remote, commit)["paper.md"].data == saved(live).file_bytes(
        next(file for file in saved(live).files if file.path == "paper.md")
    )
    assert (
        _git(remote, ["show", "-s", "--format=%an:%ae", commit]).strip()
        == b"Member:member@members.rcp.invalid"
    )


def test_push_refuses_when_own_pull_fails(sync, monkeypatch: pytest.MonkeyPatch):
    from rcp.paper.git_sync import GitError
    from rcp.paper.models import PaperPrecondition

    manager, live, remote = sync
    first = commit_remote(remote, {"paper.md": GitEntry(b"base")})
    manager.key("p", "owner/repo", "main", "member")
    manager.link("p")
    head = commit_remote(remote, {"paper.md": GitEntry(b"remote")}, first)

    async def fail(*args, **kwargs):
        raise PaperPrecondition("concurrent editor")

    monkeypatch.setattr(live, "apply", fail)
    with pytest.raises(GitError) as error:
        manager.push("p", "message", "Member", "member")
    assert str(error.value) == "retry"
    assert _git(remote, ["rev-parse", "refs/heads/main"]).decode().strip() == head
    assert manager.store.paper_git_link("p").synced_commit == first


def test_skipped_entries_survive_two_pulls_and_push(sync):
    manager, live, remote = sync
    skipped = {"link": GitEntry(b"outside", "120000"), "submodule": GitEntry(b"1" * 40, "160000")}
    first = commit_remote(remote, skipped | {"paper.md": GitEntry(b"first")})
    manager.key("p", "owner/repo", "main", "member")
    manager.link("p")
    commit_remote(remote, skipped | {"paper.md": GitEntry(b"second")}, first)
    manager.pull("p")
    human_edit(live, saved(live).files[0].file_id, "local")
    commit = manager.push("p", "message", "Member", "member")
    actual = read_tree(remote, commit)
    assert {path: actual[path] for path in skipped} == skipped
    assert actual["paper.md"].data == b"local"


def test_missing_key_reconciliation_removes_only_the_link(sync):
    manager, live, _remote = sync
    test_a_pull_merges_clean_edits_and_github_wins_an_overlap(sync)
    before = saved(live)
    (manager.directory("p") / "id_ed25519").unlink()
    manager.reconcile()
    assert manager.store.paper_git_link("p") is None
    assert saved(live).files == before.files
    assert not manager.directory("p").exists()


def test_push_retries_remote_movement_without_losing_remote_files(
    sync, monkeypatch: pytest.MonkeyPatch
):
    from rcp.paper import git_sync

    manager, live, remote = sync
    first = commit_remote(remote, {"paper.md": GitEntry(b"base")})
    manager.key("p", "owner/repo", "main", "member")
    manager.link("p")
    human_edit(live, saved(live).files[0].file_id, "local")
    original = git_sync._git
    raced = False

    def move_before_push(project, argv, **kwargs):
        nonlocal raced
        if argv[0] == "push" and not raced:
            raced = True
            commit_remote(
                remote, {"paper.md": GitEntry(b"base"), "coauthor.md": GitEntry(b"keep")}, first
            )
        return original(project, argv, **kwargs)

    monkeypatch.setattr(git_sync, "_git", move_before_push)
    commit = manager.push("p", "message", "Member", "member")
    assert read_tree(remote, commit) == {
        "paper.md": GitEntry(b"local"),
        "coauthor.md": GitEntry(b"keep"),
    }


def test_push_refuses_collision_with_skipped_remote_path(sync):
    from rcp.paper import PaperOp
    from rcp.paper.git_sync import GitError
    from tests.paper_helpers import apply

    manager, live, remote = sync
    commit_remote(remote, {"link": GitEntry(b"target", "120000")})
    manager.key("p", "owner/repo", "main", "member")
    manager.link("p")
    apply(live, [PaperOp(kind="write", path="link/file.md", data=b"local")])
    with pytest.raises(GitError) as error:
        manager.push("p", "message", "Member", "member")
    assert str(error.value) == "collision"


def test_failed_probe_stays_pending_and_reuses_key(sync, monkeypatch: pytest.MonkeyPatch):
    from rcp.paper import git_sync

    manager, _live, _remote = sync
    status = manager.key("p", "owner/repo", "main", "member")
    assert manager.key("p", "owner/repo", "main", "member")["public_key"] == status["public_key"]
    original = git_sync._git

    def refuse(project, argv, **kwargs):
        if argv[:2] == ["push", "--dry-run"]:
            raise git_sync.GitError("read only")
        return original(project, argv, **kwargs)

    monkeypatch.setattr(git_sync, "_git", refuse)
    with pytest.raises(git_sync.GitError):
        manager.link("p")
    assert manager.status("p")["state"] == "pending"
    assert manager.status("p")["last_error"]


def test_missing_key_generator_refuses_setup(sync, monkeypatch: pytest.MonkeyPatch):
    from rcp.paper.git_sync import GitError

    manager, _live, _remote = sync
    monkeypatch.setattr("rcp.paper.git_sync.shutil.which", lambda _: None)
    with pytest.raises(GitError):
        manager.key("p", "owner/repo", "main", "member")
    assert manager.store.paper_git_link("p") is None


def test_typing_during_a_pull_survives_outside_the_overlap(sync, monkeypatch: pytest.MonkeyPatch):
    from rcp.paper import git_merge

    manager, live, remote = sync
    base = "".join(f"line {i}\n" for i in range(50))
    first = commit_remote(remote, {"paper.md": GitEntry(base.encode())})
    manager.key("p", "owner/repo", "main", "member")
    manager.link("p")
    file_id = saved(live).files[0].file_id
    ours = base.replace("line 10\n", "local\n")
    human_edit(live, file_id, ours)
    commit_remote(
        remote, {"paper.md": GitEntry(base.replace("line 10\n", "remote\n").encode())}, first
    )
    original = git_merge.merge_trees

    def type_during_merge(*args):
        result = original(*args)
        monkeypatch.setattr(git_merge, "merge_trees", original)
        human_edit(live, file_id, ours.replace("line 40\n", "typed\n"))
        return result

    monkeypatch.setattr(git_merge, "merge_trees", type_during_merge)
    manager.pull("p")
    text = saved(live).files[0].text
    assert text == base.replace("line 10\n", "remote\n").replace("line 40\n", "typed\n")


def test_typing_elsewhere_during_pull_stays_unpushed(sync, monkeypatch: pytest.MonkeyPatch):
    from rcp.paper import git_merge

    manager, live, remote = sync
    first = commit_remote(remote, {"a.md": GitEntry(b"base"), "b.md": GitEntry(b"base")})
    manager.key("p", "owner/repo", "main", "member")
    manager.link("p")
    file_id = next(file.file_id for file in saved(live).files if file.path == "b.md")
    commit_remote(remote, {"a.md": GitEntry(b"remote"), "b.md": GitEntry(b"base")}, first)
    original = git_merge.merge_trees

    def type_elsewhere(*args):
        result = original(*args)
        monkeypatch.setattr(git_merge, "merge_trees", original)
        human_edit(live, file_id, "local")
        return result

    monkeypatch.setattr(git_merge, "merge_trees", type_elsewhere)
    manager.pull("p")
    assert manager.status("p")["unpushed"]


def test_vanished_remote_branch_refuses_and_keeps_the_paper(sync):
    from rcp.paper.git_sync import GitError

    manager, live, remote = sync
    first = commit_remote(remote, {"paper.md": GitEntry(b"base")})
    manager.key("p", "owner/repo", "main", "member")
    manager.link("p")
    _git(remote, ["update-ref", "-d", "refs/heads/main"])
    with pytest.raises(GitError):
        manager.pull("p")
    assert saved(live).files[0].text == "base"
    assert manager.store.paper_git_link("p").synced_commit == first
    with pytest.raises(GitError) as error:
        manager.push("p", "message", "Member", "member")
    assert str(error.value) != "retry"
    assert manager.status("p")["last_error"]
    _git(remote, ["update-ref", "refs/heads/main", first])
    manager.pull("p")
    status = manager.status("p")
    assert not status["last_error"]
    assert status["checked_at"]


def test_network_git_runs_outside_project_admission(sync, monkeypatch: pytest.MonkeyPatch):
    from contextlib import contextmanager

    manager, live, remote = sync
    admitted = []
    network = []

    @contextmanager
    def admission(_):
        admitted.append(True)
        try:
            yield
        finally:
            admitted.pop()

    original = git_sync._git

    def watch(directory, args, *rest, **kwargs):
        if args[0] in {"ls-remote", "fetch", "push"}:
            network.append((args[0], bool(admitted)))
        return original(directory, args, *rest, **kwargs)

    manager.admission = admission
    monkeypatch.setattr(git_sync, "_git", watch)
    first = commit_remote(remote, {"paper.md": GitEntry(b"base")})
    manager.key("p", "owner/repo", "main", "member")
    manager.link("p")
    commit_remote(remote, {"paper.md": GitEntry(b"remote")}, first)
    manager.pull("p")
    human_edit(live, saved(live).files[0].file_id, "local")
    manager.push("p", "message", "Member", "member")
    assert {name for name, _ in network} == {"ls-remote", "fetch", "push"}
    assert not any(held for _, held in network)


def test_edit_reverted_to_the_synced_text_is_not_unpushed(sync):
    manager, live, remote = sync
    commit_remote(remote, {"paper.md": GitEntry(b"base")})
    manager.key("p", "owner/repo", "main", "member")
    manager.link("p")
    file = saved(live).files[0]
    assert not manager.status("p")["unpushed"]
    objects = manager.directory("p") / "repo.git" / "objects"
    stored = sorted(path for path in objects.rglob("*") if path.is_file())
    human_edit(live, file.file_id, "edited")
    assert manager.status("p")["unpushed"]
    assert manager.changed_files("p")[0]["path"] == "paper.md"
    assert sorted(path for path in objects.rglob("*") if path.is_file()) == stored
    human_edit(live, file.file_id, "base")
    assert not manager.status("p")["unpushed"]
    assert manager.changed_files("p") == []


def test_one_changed_file_diff_is_bounded(sync, monkeypatch: pytest.MonkeyPatch):
    manager, live, remote = sync
    commit_remote(remote, {"paper.md": GitEntry(b"one\ntwo\nthree\n")})
    manager.key("p", "owner/repo", "main", "member")
    manager.link("p")
    human_edit(live, saved(live).files[0].file_id, "one\n2\nthree\n")
    diff = manager.file_diff("p", "paper.md")
    assert [line for line in diff["lines"] if line[0] in "+-"] == ["-two", "+2"]
    assert diff["lines"][0].startswith("@@")
    assert (diff["binary"], diff["more"]) == (False, 0)
    monkeypatch.setattr(git_sync, "PAPER_GIT_DIFF_LINES", 2)
    capped = manager.file_diff("p", "paper.md")
    assert len(capped["lines"]) == 2 and capped["more"] > 0
    assert manager.file_diff("p", "missing.md")["lines"] == []


def test_switch_branch_carries_unpushed_edits_onto_the_branch_files(sync):
    manager, live, remote = sync
    base = "".join(f"line {i}\n" for i in range(30))
    main = commit_remote(
        remote, {"paper.md": GitEntry(base.encode()), "main-only.md": GitEntry(b"main")}
    )
    review = build_commit(
        remote,
        {
            "paper.md": GitEntry(base.replace("line 25\n", "reviewed\n").encode()),
            "review.md": GitEntry(b"review"),
        },
        main,
        "review",
        "Coauthor",
        "coauthor",
    )
    _git(remote, ["update-ref", "refs/heads/review", review])
    manager.key("p", "owner/repo", "main", "member")
    manager.link("p")
    assert manager.branches("p") == ["main", "review"]
    paper = next(file for file in saved(live).files if file.path == "paper.md")
    human_edit(live, paper.file_id, base.replace("line 5\n", "local\n"))

    status = manager.switch("p", "review", create=False)

    files = {file.path: file.text for file in saved(live).files}
    assert set(files) == {"paper.md", "review.md"}
    assert "local\n" in files["paper.md"] and "reviewed\n" in files["paper.md"]
    assert (status["branch"], status["unpushed"]) == ("review", True)
    commit = manager.push("p", "carry", "Member", "member")
    assert _git(remote, ["rev-parse", "refs/heads/review"]).decode().strip() == commit
    assert _git(remote, ["rev-parse", "refs/heads/main"]).decode().strip() == main


def test_new_branch_starts_at_the_synced_commit_and_takes_the_next_push(sync):
    manager, live, remote = sync
    main = commit_remote(remote, {"paper.md": GitEntry(b"draft\n")})
    manager.key("p", "owner/repo", "main", "member")
    manager.link("p")
    human_edit(live, saved(live).files[0].file_id, "edited\n")

    status = manager.switch("p", "camera-ready", create=True)

    assert (status["branch"], status["unpushed"]) == ("camera-ready", True)
    assert saved(live).files[0].text == "edited\n"
    assert _git(remote, ["rev-parse", "refs/heads/camera-ready"]).decode().strip() == main
    commit = manager.push("p", "edit", "Member", "member")
    assert read_tree(remote, commit)["paper.md"].data == b"edited\n"
    assert _git(remote, ["rev-parse", "refs/heads/main"]).decode().strip() == main


def test_switch_refusals_keep_the_current_branch(sync):
    from rcp.paper.git_sync import GitError

    manager, live, remote = sync
    commit_remote(remote, {"paper.md": GitEntry(b"draft\n")})
    manager.key("p", "owner/repo", "main", "member")
    manager.link("p")
    with pytest.raises(GitError):
        manager.switch("p", "missing", create=False)
    with pytest.raises(GitError):
        manager.switch("p", "main", create=True)
    with pytest.raises(GitError):
        manager.switch("p", "bad..name", create=True)
    status = manager.status("p")
    assert (status["branch"], status["last_error"]) == ("main", "")
    assert saved(live).files[0].text == "draft\n"


def test_same_repository_reuses_the_project_key_and_links_at_once(tmp_path: Path, sync):
    manager, live, remote = sync
    commit_remote(remote, {"paper.md": GitEntry(b"from github\n")})
    shared = tmp_path / "credentials" / "code" / "id_ed25519"
    git_sync.generate_key(shared.parent, "p")
    manager.shared_key = lambda project, repository: (
        ("code", shared) if (project, repository) == ("p", "Owner/Repo") else None
    )
    status = manager.key("p", "Owner/Repo", "main", "member")
    assert (status["state"], status["key_source"], status["public_key"]) == ("linked", "code", "")
    assert saved(live).files[0].text == "from github\n"
    assert (manager.directory("p") / "id_ed25519").resolve() == shared.resolve()
    manager.disconnect("p")
    assert shared.is_file()
    assert manager.key("p", "owner/other", "main", "member")["state"] == "pending"


def test_shared_deploy_key_lends_only_a_ready_backend_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from types import SimpleNamespace

    def record(identity: str, *, verified: bool = True, machine: str = "backend"):
        return SimpleNamespace(
            repository=SimpleNamespace(identity=identity),
            git_check=SimpleNamespace(write_verified=verified),
            machine_id=machine,
        )

    sources = {
        "a": record("owner/repo", verified=False),
        "b": record("owner/repo", machine="gpu"),
        "c": record("owner/other"),
        "d": record("owner/repo"),
        "e": record("owner/repo"),
    }
    hosts = {"backend": "", "gpu": "gpu.example"}
    monkeypatch.setattr("rcp.provisioning_repositories.team_repository_sources", lambda *_: sources)
    monkeypatch.setattr("rcp.machine_cards.host_of", lambda _store, machine: hosts[machine])
    layout = SimpleNamespace(
        project_deploy_key_path=lambda project, alias: tmp_path / project / alias / "id_ed25519"
    )
    for alias in "abce":
        key = tmp_path / "p" / alias / "id_ed25519"
        key.parent.mkdir(parents=True)
        key.write_text("key")
    found = git_sync.shared_deploy_key(None, layout, "p", "Owner/Repo")
    assert found == ("e", tmp_path / "p" / "e" / "id_ed25519")
    assert git_sync.shared_deploy_key(None, layout, "p", "owner/none") is None


def test_blank_branch_links_the_remote_default_branch(sync):
    manager, live, remote = sync
    commit = build_commit(remote, {"paper.md": GitEntry(b"default\n")}, "", "init", "C", "c")
    _git(remote, ["update-ref", "refs/heads/master", commit])
    _git(remote, ["update-ref", "refs/heads/gh-pages", commit])
    _git(remote, ["symbolic-ref", "HEAD", "refs/heads/master"])
    assert manager.key("p", "owner/repo", "", "member")["branch"] == ""
    status = manager.link("p")
    assert (status["state"], status["branch"]) == ("linked", "master")
    assert saved(live).files[0].text == "default\n"


def test_typed_branch_missing_from_remote_is_refused_with_the_branches(sync):
    from rcp.paper.git_sync import GitError

    manager, live, remote = sync
    commit = build_commit(remote, {"paper.md": GitEntry(b"x")}, "", "init", "C", "c")
    names = [f"b{i}" for i in range(7)]
    for name in names:
        _git(remote, ["update-ref", "refs/heads/" + name, commit])
    manager.key("p", "owner/repo", "main", "member")
    with pytest.raises(GitError) as error:
        manager.link("p")
    assert all(name in str(error.value) for name in names[:5])
    assert not any(name in str(error.value) for name in names[5:])
    status = manager.status("p")
    assert (status["state"], status["branch"]) == ("pending", "main")
    assert status["last_error"] == str(error.value)
    assert saved(live).files == []


def test_empty_repository_links_main_and_the_first_push_creates_it(sync):
    manager, live, remote = sync
    manager.key("p", "owner/repo", "", "member")
    assert manager.link("p")["branch"] == "main"
    from rcp.paper import PaperOp
    from tests.paper_helpers import apply

    apply(live, [PaperOp(kind="write", path="paper.md", data=b"first")])
    commit = manager.push("p", "first", "Member", "member")
    assert _git(remote, ["rev-parse", "refs/heads/main"]).decode().strip() == commit


def test_unsynced_link_on_a_missing_branch_errors_on_pull(sync):
    from rcp.paper.git_sync import GitError

    manager, live, remote = sync
    manager.key("p", "owner/repo", "main", "member")
    manager.link("p")
    commit = build_commit(remote, {"paper.md": GitEntry(b"x")}, "", "init", "C", "c")
    _git(remote, ["update-ref", "refs/heads/master", commit])
    with pytest.raises(GitError) as error:
        manager.pull("p")
    assert "master" in str(error.value)
    assert manager.status("p")["last_error"] == str(error.value)
    assert saved(live).files == []


def test_key_on_a_linked_paper_refuses_and_keeps_the_link(sync):
    from rcp.paper.git_sync import GitError

    manager, live, remote = sync
    first = commit_remote(remote, {"paper.md": GitEntry(b"base")})
    manager.key("p", "owner/repo", "main", "member")
    manager.link("p")
    with pytest.raises(GitError):
        manager.key("p", "owner/other", "main", "member")
    link = manager.store.paper_git_link("p")
    assert (link.state, link.repository, link.synced_commit) == ("linked", "owner/repo", first)
    assert (manager.directory("p") / "id_ed25519").is_file()


def test_failed_first_merge_keeps_the_link_pending(sync, monkeypatch: pytest.MonkeyPatch):
    from rcp.paper import git_merge

    manager, live, remote = sync
    head = commit_remote(remote, {"paper.md": GitEntry(b"base")})
    manager.key("p", "owner/repo", "main", "member")
    original = git_merge.merge_trees

    def fail(*args):
        raise RuntimeError("merge failed")

    monkeypatch.setattr(git_merge, "merge_trees", fail)
    with pytest.raises(RuntimeError):
        manager.link("p")
    link = manager.store.paper_git_link("p")
    assert (link.state, link.synced_commit, link.last_error) == ("pending", "", "merge failed")
    monkeypatch.setattr(git_merge, "merge_trees", original)
    assert manager.link("p")["state"] == "linked"
    assert manager.store.paper_git_link("p").synced_commit == head


def test_reconcile_removes_key_folders_without_a_link(sync, caplog: pytest.LogCaptureFixture):
    manager, _live, remote = sync
    commit_remote(remote, {"paper.md": GitEntry(b"base")})
    manager.key("p", "owner/repo", "main", "member")
    manager.link("p")
    orphan = manager.directory("orphan")
    git_sync.generate_key(orphan, "orphan")
    (manager.root / ".DS_Store").write_text("")
    (manager.directory("p") / "id_ed25519").unlink()
    with caplog.at_level("WARNING"):
        manager.reconcile()
    assert not orphan.exists()
    assert not manager.directory("p").exists()
    assert manager.store.paper_git_link("p") is None
    assert any(record.name == git_sync.__name__ for record in caplog.records)
