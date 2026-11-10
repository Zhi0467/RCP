"""Bare Git transport for the SQLite-owned Paper workspace."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from rcp import limits
from rcp.git_identity import member_git_email
from rcp.limits import PAPER_GIT_TIMEOUT_SECONDS


@dataclass(frozen=True)
class GitEntry:
    data: bytes
    mode: str = "100644"
    file_id: str = field(default="", compare=False)
    oid: str = field(default="", compare=False)
    size: int | None = None


class GitError(ValueError):
    def __init__(self, message: str, returncode: int = 128) -> None:
        super().__init__(message)
        self.returncode = returncode


def remote_url(repository: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repository):
        raise ValueError("Repository must be owner/name on GitHub")
    return f"git@github.com:{repository}.git"


def _git(
    project: Path,
    argv: list[str],
    *,
    timeout: float = PAPER_GIT_TIMEOUT_SECONDS,
    data: bytes | None = None,
    environment: dict[str, str] | None = None,
) -> bytes:
    project.mkdir(parents=True, exist_ok=True, mode=0o700)
    scratch = project / "tmp"
    scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(scratch),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_VARIANT": "ssh",
        "GIT_SSH_COMMAND": shlex.join(
            [
                "ssh",
                "-F",
                os.devnull,
                "-i",
                str(project / "id_ed25519"),
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"GlobalKnownHostsFile={os.devnull}",
                "-o",
                f"UserKnownHostsFile={Path(__file__).with_name('github_known_hosts')}",
            ]
        ),
        **(environment or {}),
    }
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"core.hooksPath={os.devnull}",
                "--git-dir",
                str(project / "repo.git"),
                *argv,
            ],
            input=data,
            capture_output=True,
            env=env,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(str(exc)) from exc
    if result.returncode:
        error = result.stderr.decode(errors="replace").strip() or f"Git {argv[0]} failed"
        if "Host key verification failed" in error:
            error += "; GitHub host key rotation requires an RCP release"
        raise GitError(error, result.returncode)
    return result.stdout


def generate_key(project: Path, project_id: str) -> str:
    executable = shutil.which("ssh-keygen")
    if executable is None:
        raise GitError("ssh-keygen is unavailable")
    project.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = project / "id_ed25519"
    subprocess.run(
        [
            executable,
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            f"rcp:paper:{project_id}",
            "-f",
            str(key),
        ],
        check=True,
        capture_output=True,
        timeout=PAPER_GIT_TIMEOUT_SECONDS,
        umask=0o077,
    )
    return subprocess.run(
        [executable, "-lf", str(key.with_suffix(".pub")), "-E", "sha256"],
        check=True,
        capture_output=True,
        text=True,
        timeout=PAPER_GIT_TIMEOUT_SECONDS,
    ).stdout.split()[1]


def read_tree(project: Path, commit: str) -> dict[str, GitEntry]:
    if not commit:
        return {}
    entries: list[tuple[str, str, bytes, bytes, int]] = []
    for record in _git(project, ["ls-tree", "-r", "-l", "-z", commit]).split(b"\0"):
        if not record:
            continue
        metadata, path = record.split(b"\t", 1)
        mode, kind, oid, size = metadata.split()
        entries.append(
            (
                path.decode("utf-8", errors="surrogateescape"),
                mode.decode(),
                kind,
                oid,
                int(size) if size != b"-" else 0,
            )
        )
    objects = [
        oid
        for _, _, kind, oid, size in entries
        if kind == b"blob" and size <= limits.PAPER_MAX_UPLOAD_BYTES
    ]
    body = (
        _git(project, ["cat-file", "--batch"], data=b"\n".join(objects) + b"\n") if objects else b""
    )
    blobs: dict[bytes, bytes] = {}
    offset = 0
    for oid in objects:
        end = body.index(b"\n", offset)
        size = int(body[offset:end].split()[2])
        blobs[oid] = body[end + 1 : end + 1 + size]
        offset = end + size + 2
    return {
        path: GitEntry(blobs.get(oid, oid), mode)
        if kind != b"blob" or oid in blobs
        else GitEntry(b"", mode, oid=oid.decode(), size=size)
        for path, mode, kind, oid, size in entries
    }


def write_tree(
    project: Path, tree: dict[str, GitEntry], environment: dict[str, str] | None = None
) -> str:
    scratch = project / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch) as directory:
        env = {**(environment or {}), "GIT_INDEX_FILE": str(Path(directory) / "index")}
        _git(project, ["read-tree", "--empty"], environment=env)
        records = bytearray()
        for path, entry in sorted(tree.items()):
            oid = (
                entry.oid.encode()
                if entry.oid
                else entry.data
                if entry.mode == "160000"
                else _git(
                    project, ["hash-object", "-w", "--stdin"], data=entry.data, environment=env
                ).strip()
            )
            records.extend(
                entry.mode.encode()
                + b" "
                + oid
                + b"\t"
                + path.encode("utf-8", errors="surrogateescape")
                + b"\0"
            )
        if records:
            _git(
                project,
                ["update-index", "-z", "--index-info"],
                data=bytes(records),
                environment=env,
            )
        return _git(project, ["write-tree"], environment=env).decode().strip()


def build_commit(
    project: Path, tree: dict[str, GitEntry], parent: str, message: str, name: str, user_id: str
) -> str:
    env = {
        f"GIT_{role}_{key}": value
        for role in ("AUTHOR", "COMMITTER")
        for key, value in (("NAME", name), ("EMAIL", member_git_email(user_id)))
    }
    return (
        _git(
            project,
            ["commit-tree", write_tree(project, tree), *(["-p", parent] if parent else [])],
            data=message.encode(),
            environment=env,
        )
        .decode()
        .strip()
    )


def snapshot_tree(snapshot):
    return {
        file.path: GitEntry(snapshot.file_bytes(file), file_id=file.file_id)
        for file in snapshot.files
    }


class PaperGit:
    def __init__(self, store, live, data_dir, admission):
        from contextlib import nullcontext
        from threading import RLock

        from rcp.paper.changes import PaperChanges

        self.store = store
        self.live = live
        self.root = data_dir / "paper-git"
        self.admission = admission
        self.changes = PaperChanges(store, live, lambda _: nullcontext())
        self._locks = {}
        self._locks_guard = RLock()

    def directory(self, project_id: str) -> Path:
        if not project_id or Path(project_id).name != project_id or project_id in {".", ".."}:
            raise ValueError("Invalid project id")
        return self.root / project_id

    def lock(self, project_id: str):
        from threading import RLock

        with self._locks_guard:
            return self._locks.setdefault(project_id, RLock())

    def _record(self, project_id: str):
        link = self.store.paper_git_link(project_id)
        if link is None:
            raise GitError("Paper is not linked")
        return link

    def _active_held(self, link):
        with self.store.connection() as connection:
            undecided = {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT json_extract(c.payload,'$.file_id') FROM paper_changes c "
                    "JOIN paper_change_groups g ON g.project_id=c.project_id AND g.group_id=c.group_id "
                    "WHERE g.project_id=? AND g.task_id='paper-git' AND g.state IN ('pending','outdated') "
                    "AND NOT EXISTS (SELECT 1 FROM paper_change_receipts r "
                    "WHERE r.project_id=g.project_id AND r.group_id=g.group_id AND r.action='accept' "
                    "AND json_type(r.payload,'$.receipt') IS NOT NULL)",
                    (link.project_id,),
                )
            }
        return [
            item for item in link.held if item["kind"] == "file" or item["file_id"] in undecided
        ]

    def _release(self, link):
        held = self._active_held(link)
        if held != link.held:
            link.held = held
            self.store.save_paper_git_link(link)
        return link

    def status(self, project_id: str):
        with self.lock(project_id):
            link = self.store.paper_git_link(project_id)
            if link is None:
                return {
                    "state": "none",
                    "repository": "",
                    "branch": "",
                    "public_key": "",
                    "head": {"commit": "", "author": ""},
                    "unpushed": False,
                    "conflicts": 0,
                    "skipped": [],
                    "last_error": "",
                    "checked_at": "",
                }
            author = ""
            if link.synced_commit:
                author = _git(
                    self.directory(project_id), ["show", "-s", "--format=%an", link.synced_commit]
                ).decode(errors="replace")
                author = "".join(char for char in author if char.isprintable())[
                    : limits.PAPER_GIT_AUTHOR_MAX_CHARS
                ]
            key = self.directory(project_id) / "id_ed25519.pub"
            snapshot = self.live.run_sync(self.live.snapshot(project_id))
            return {
                "state": link.state,
                "repository": link.repository,
                "branch": link.branch,
                "public_key": key.read_text() if key.exists() else "",
                "head": {"commit": link.synced_commit, "author": author},
                "unpushed": snapshot.revision != link.synced_revision
                and self._differs(link, snapshot),
                "conflicts": len(self._active_held(link)),
                "skipped": link.skipped,
                "last_error": link.last_error,
                "checked_at": link.checked_at,
            }

    def key(self, project_id: str, repository: str, branch: str, actor: str):
        from rcp.storage.paper_git_links import PaperGitLink

        remote_url(repository)
        directory = self.directory(project_id)
        with self.lock(project_id), self.admission(project_id):
            _git(directory, ["check-ref-format", "refs/heads/" + branch])
            existing = self.store.paper_git_link(project_id)
            if existing and existing.state == "pending":
                if (existing.repository, existing.branch) != (repository, branch):
                    raise GitError("Unlink the pending repository before changing it")
                return self.status(project_id)
            if existing or (directory / "id_ed25519").exists():
                self._unlink(project_id)
            fingerprint = generate_key(directory, project_id)
            _git(directory, ["init", "--bare", "--template=", str(directory / "repo.git")])
            self.store.save_paper_git_link(
                PaperGitLink(
                    project_id=project_id,
                    repository=repository,
                    branch=branch,
                    key_fingerprint=fingerprint,
                    linked_by=actor,
                    linked_at=self.store.now(),
                )
            )
            return self.status(project_id)

    def _unlink(self, project_id: str) -> None:
        with self.store.connection() as connection:
            connection.execute(
                "DELETE FROM paper_change_groups WHERE project_id=? AND task_id='paper-git'",
                (project_id,),
            )
            connection.execute("DELETE FROM paper_git_links WHERE project_id=?", (project_id,))
        directory = self.directory(project_id)
        if directory.exists():
            shutil.rmtree(directory)

    def disconnect(self, project_id: str) -> None:
        with self.lock(project_id), self.admission(project_id):
            self._unlink(project_id)

    def reconcile(self) -> None:
        for link in self.store.paper_git_links():
            if not (self.directory(link.project_id) / "id_ed25519").is_file():
                with self.lock(link.project_id):
                    self._unlink(link.project_id)

    def _head(self, link) -> str:
        output = _git(
            self.directory(link.project_id),
            ["ls-remote", remote_url(link.repository), "refs/heads/" + link.branch],
        )
        return output.split()[0].decode() if output else ""

    def link(self, project_id: str):
        with self.lock(project_id):
            link = self._record(project_id)
            try:
                directory = self.directory(project_id)
                probe = build_commit(directory, {}, "", "Paper link probe", "RCP", "paper")
                _git(
                    directory,
                    [
                        "push",
                        "--dry-run",
                        remote_url(link.repository),
                        f"{probe}:refs/heads/rcp-link-probe-{uuid.uuid4().hex}",
                    ],
                )
                head = self._fetch(link)
                with self.admission(project_id):
                    link.state = "linked"
                    self.store.save_paper_git_link(link)
                    self._merge(link, head)
            except Exception as exc:
                self._error(project_id, exc)
                raise
            return self.status(project_id)

    def _error(self, project_id: str, error: Exception) -> None:
        link = self._record(project_id)
        link.last_error = str(error)
        self.store.save_paper_git_link(link)

    def pull(self, project_id: str, *, refresh: bool = False):
        with self.lock(project_id):
            link = self._record(project_id)
            if link.state != "linked":
                raise GitError("Paper link is pending")
            try:
                head = self._fetch(link)
                with self.admission(project_id):
                    self._merge(link, head, refresh=refresh)
            except Exception as exc:
                self._error(project_id, exc)
                raise
            return self.status(project_id)

    def _budget(self, directory: Path) -> None:
        from rcp.limits import PAPER_GIT_MAX_REPOSITORY_BYTES

        if (
            sum(
                path.stat().st_size
                for path in (directory / "repo.git").rglob("*")
                if path.is_file()
            )
            > PAPER_GIT_MAX_REPOSITORY_BYTES
        ):
            raise GitError("Paper Git repository exceeds its disk limit")

    def _pin(self, link) -> None:
        directory = self.directory(link.project_id)
        desired = {
            "refs/rcp/held/" + item["base_commit"]: item["base_commit"]
            for item in link.held
            if item["base_commit"]
        }
        if link.synced_commit:
            desired["refs/rcp/synced"] = link.synced_commit
        existing = (
            _git(directory, ["for-each-ref", "--format=%(refname)", "refs/rcp/"])
            .decode()
            .splitlines()
        )
        for ref, commit in desired.items():
            _git(directory, ["update-ref", ref, commit])
        for ref in set(existing) - desired.keys():
            _git(directory, ["update-ref", "-d", ref])
        _git(directory, ["reflog", "expire", "--expire=now", "--all"])
        _git(directory, ["gc", "--prune=now"])

    def _fetch(self, link) -> str:
        directory = self.directory(link.project_id)
        self._budget(directory)
        head = self._head(link)
        if not head and link.synced_commit:
            raise GitError("Paper Git branch is missing from the remote")
        if head and head != link.synced_commit:
            _git(
                directory,
                [
                    "fetch",
                    "--depth=1",
                    "--no-tags",
                    remote_url(link.repository),
                    "refs/heads/" + link.branch,
                ],
            )
            head = _git(directory, ["rev-parse", "FETCH_HEAD"]).decode().strip()
            _git(directory, ["update-ref", "refs/rcp/incoming", head])
            try:
                self._budget(directory)
            except GitError:
                _git(directory, ["update-ref", "-d", "refs/rcp/incoming"])
                _git(directory, ["gc", "--prune=now"])
                raise
        return head

    def _base(self, link) -> dict[str, GitEntry]:
        directory = self.directory(link.project_id)
        base = read_tree(directory, link.synced_commit)
        for item in link.skipped:
            base.pop(item["path"], None)
        for item in link.held:
            ancestor = read_tree(directory, item["base_commit"]).get(item["path"])
            if ancestor is None:
                base.pop(item["path"], None)
            else:
                base[item["path"]] = ancestor
        return base

    def _apply(self, link, merged, before):
        from rcp.paper.models import PaperPrecondition
        from rcp.regular_tree_reader import RegularTree

        revision = before.revision
        if merged.ops:
            revision = self.live.run_sync(
                self.live.apply(
                    link.project_id,
                    merged.ops,
                    "github",
                    {"checks": merged.checks},
                    checkpoint=True,
                )
            ).revision
        after = self.live.run_sync(self.live.snapshot(link.project_id))
        current = snapshot_tree(after)
        if any(current.get(path) != merged.tree[path] for path in merged.proposals):
            raise PaperPrecondition("Paper conflict text changed during merge")
        paths = {item["path"] for item in link.held + merged.held}
        groups = {
            group.group_id
            for group in self.changes.groups(link.project_id, "paper-git")
            if any(change.path in paths for change in group.changes)
        }
        self.changes.collect(
            link.project_id,
            "paper-git",
            after,
            RegularTree({"files/" + path: body for path, body in merged.proposals.items()}, []),
            replace_group_ids=groups,
        )
        return revision, after

    def _settled(self, link, head: str, merged, before, revision: int, after) -> None:
        held_bases = {item["path"]: item["base_commit"] for item in link.held}
        files = {file.path: file.file_id for file in after.files}
        for item in merged.held:
            item["base_commit"] = held_bases.get(item["path"], link.synced_commit)
            item["file_id"] = files.get(item["path"], "")
        link.held = merged.held
        link.held = self._active_held(link)
        link.skipped = merged.skipped
        link.synced_commit = head
        if before.revision == link.synced_revision and revision == before.revision + bool(
            merged.ops
        ):
            link.synced_revision = revision
        self._checked(link)
        self._pin(link)

    def _checked(self, link) -> None:
        link.last_error = ""
        link.checked_at = self.store.now()
        self.store.save_paper_git_link(link)

    def _merge(self, link, head: str, *, refresh: bool = False) -> None:
        from rcp.limits import PAPER_GIT_APPLY_RETRIES
        from rcp.paper.changes import InvalidChanges
        from rcp.paper.git_merge import merge_trees
        from rcp.paper.models import PaperLimit, PaperPrecondition, PaperRateLimit

        link = self._release(link)
        if head == link.synced_commit and not refresh:
            self._checked(link)
            return
        theirs = read_tree(self.directory(link.project_id), head)
        base = self._base(link)
        for attempt in range(PAPER_GIT_APPLY_RETRIES):
            before = self.live.run_sync(self.live.snapshot(link.project_id))
            merged = merge_trees(base, theirs, snapshot_tree(before))
            try:
                revision, after = self._apply(link, merged, before)
            except PaperRateLimit:
                raise
            except (PaperPrecondition, PaperLimit, InvalidChanges) as exc:
                raced = not isinstance(exc, InvalidChanges) or isinstance(
                    exc.__cause__, (PaperPrecondition, PaperLimit)
                )
                if not raced or attempt + 1 == PAPER_GIT_APPLY_RETRIES:
                    raise
                continue
            self._settled(link, head, merged, before, revision, after)
            return

    def decide(self, project_id: str, group_id: str, decision: str, actor: str):
        with self.lock(project_id), self.admission(project_id):
            link = self._release(self._record(project_id))
            group = self.changes.group(project_id, group_id)
            if group.task_id != "paper-git":
                raise KeyError(group_id)
            if decision == "accept":
                self.changes.accept(project_id, group_id, actor)
            elif decision == "reject":
                self.changes.reject(project_id, group_id, actor)
            else:
                raise ValueError("Invalid conflict decision")
            self._release(link)
            self._pin(link)
            return self.status(project_id)

    def conflicts(self, project_id: str):
        with self.lock(project_id):
            link = self._record(project_id)
            theirs = read_tree(self.directory(project_id), link.synced_commit)
            ours = snapshot_tree(self.live.run_sync(self.live.snapshot(project_id)))
            return [
                {
                    "path": item["path"],
                    "kind": "file",
                    "ours": "present" if item["path"] in ours else "absent",
                    "theirs": "present" if item["path"] in theirs else "absent",
                }
                for item in link.held
                if item["kind"] == "file"
            ]

    def resolve(self, project_id: str, path: str, take: str, actor: str):
        from rcp.paper.git_merge import tree_ops

        with self.lock(project_id), self.admission(project_id):
            link = self._release(self._record(project_id))
            if not any(item["path"] == path and item["kind"] == "file" for item in link.held):
                raise KeyError(path)
            if take == "theirs":
                theirs = read_tree(self.directory(project_id), link.synced_commit)
                ours = snapshot_tree(self.live.run_sync(self.live.snapshot(project_id)))
                target = dict(ours)
                target.pop(path, None)
                if path in theirs:
                    target[path] = theirs[path]
                ops, checks = tree_ops(ours, target)
                if ops:
                    self.live.run_sync(
                        self.live.apply(project_id, ops, actor, {"checks": checks}, checkpoint=True)
                    )
            elif take != "mine":
                raise ValueError("Invalid conflict decision")
            link.held = [item for item in link.held if item["path"] != path]
            self.store.save_paper_git_link(link)
            self._pin(link)
            return self.status(project_id)

    def _push_tree(self, link, snapshot):
        remote = read_tree(self.directory(link.project_id), link.synced_commit)
        skipped = {item["path"] for item in link.skipped}
        ours = snapshot_tree(snapshot)
        for path in ours:
            if any(
                path == other or path.startswith(other + "/") or other.startswith(path + "/")
                for other in skipped
            ):
                raise GitError("collision")
        return {path: entry for path, entry in remote.items() if path in skipped} | {
            path: GitEntry(entry.data, remote[path].mode if path in remote else "100644")
            for path, entry in ours.items()
        }

    def _differs(self, link, snapshot) -> bool:
        try:
            ours = self._push_tree(link, snapshot)
        except GitError:
            return True
        return ours != read_tree(self.directory(link.project_id), link.synced_commit)

    def changed_files(self, project_id: str):
        with self.lock(project_id):
            link = self._record(project_id)
            directory = self.directory(project_id)
            snapshot = self.live.run_sync(self.live.snapshot(project_id))
            tree = self._push_tree(link, snapshot)
            with tempfile.TemporaryDirectory(dir=directory / "tmp") as scratch:
                env = {
                    "GIT_OBJECT_DIRECTORY": scratch,
                    "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(directory / "repo.git" / "objects"),
                }
                target = write_tree(directory, tree, env)
                base = link.synced_commit or write_tree(directory, {}, env)
                output = _git(
                    directory,
                    ["diff", "--no-renames", "--numstat", "-z", base, target],
                    environment=env,
                )
            remote = read_tree(directory, link.synced_commit)
            changed = []
            for record in output.split(b"\0"):
                if not record:
                    continue
                added, removed, name = record.split(b"\t", 2)
                path = name.decode("utf-8", errors="replace")
                changed.append(
                    {
                        "path": path,
                        "status": "added"
                        if path not in remote
                        else "deleted"
                        if path not in tree
                        else "modified",
                        "added": int(added) if added != b"-" else None,
                        "removed": int(removed) if removed != b"-" else None,
                    }
                )
            return changed

    def push(self, project_id: str, message: str, name: str, user_id: str) -> str:
        if not message.strip() or "\0" in message:
            raise ValueError("A commit message is required")
        with self.lock(project_id):
            for attempt in range(limits.PAPER_GIT_PUSH_ATTEMPTS):
                link = self._record(project_id)
                if link.state != "linked":
                    raise GitError("Paper link is pending")
                directory = self.directory(project_id)
                try:
                    head = self._fetch(link)
                except Exception as exc:
                    self._error(project_id, exc)
                    raise
                with self.admission(project_id):
                    try:
                        self._merge(link, head)
                    except Exception as exc:
                        self._error(project_id, exc)
                        raise GitError("retry") from exc
                    link = self._release(self._record(project_id))
                    if link.held:
                        raise GitError("conflicts")
                    snapshot = self.live.run_sync(self.live.snapshot(project_id))
                    tree = self._push_tree(link, snapshot)
                    commit = build_commit(
                        directory, tree, link.synced_commit, message, name, user_id
                    )
                try:
                    _git(
                        directory,
                        [
                            "push",
                            remote_url(link.repository),
                            commit + ":refs/heads/" + link.branch,
                        ],
                    )
                except GitError as exc:
                    if attempt + 1 < limits.PAPER_GIT_PUSH_ATTEMPTS:
                        continue
                    self._error(project_id, exc)
                    raise
                with self.admission(project_id):
                    link.synced_commit = commit
                    link.synced_revision = snapshot.revision
                    link.last_error = ""
                    link.checked_at = self.store.now()
                    self.store.save_paper_git_link(link)
                    self._pin(link)
                return commit
        raise GitError("retry")
