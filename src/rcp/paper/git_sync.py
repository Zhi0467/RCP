"""Bare Git transport for the Paper folder; GitHub wins every overlap on apply."""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from itertools import dropwhile
from pathlib import Path

from rcp import limits
from rcp.git_identity import member_git_email
from rcp.limits import PAPER_GIT_DIFF_LINES, PAPER_GIT_TIMEOUT_SECONDS

_LOG = logging.getLogger(__name__)


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


def shared_deploy_key(store, layout, project_id: str, repository: str) -> tuple[str, Path] | None:
    """A ready, write-verified project repository on the backend's own machine
    that is the same GitHub repository lends its deploy key to Paper."""
    from rcp.machine_cards import host_of
    from rcp.provisioning_repositories import team_repository_sources

    sources = team_repository_sources(store, project_id)
    for alias, record in sorted((sources or {}).items()):
        if (
            record.repository is None
            or record.repository.identity != repository.lower()
            or not record.git_check.write_verified
        ):
            continue
        try:
            if host_of(store, record.machine_id):
                continue
        except KeyError:
            continue
        key = layout.project_deploy_key_path(project_id, alias)
        if key.is_file():
            return alias, key
    return None


class PaperGit:
    def __init__(self, store, live, data_dir, admission, shared_key=lambda _p, _r: None):
        from threading import RLock

        self.store = store
        self.live = live
        self.root = data_dir / "paper-git"
        self.admission = admission
        self.shared_key = shared_key
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

    def status(self, project_id: str):
        with self.lock(project_id):
            link = self.store.paper_git_link(project_id)
            if link is None:
                return {
                    "state": "none",
                    "repository": "",
                    "branch": "",
                    "public_key": "",
                    "key_source": "",
                    "head": {"commit": "", "author": ""},
                    "unpushed": False,
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
            own = key.with_suffix("")
            source = own.readlink().parent.name if own.is_symlink() else ""
            snapshot = self.live.run_sync(self.live.snapshot(project_id))
            return {
                "state": link.state,
                "repository": link.repository,
                "branch": link.branch,
                "public_key": "" if source or not key.exists() else key.read_text(),
                "key_source": source,
                "head": {"commit": link.synced_commit, "author": author},
                "unpushed": self._differs(link, snapshot),
                "skipped": link.skipped,
                "last_error": link.last_error,
                "checked_at": link.checked_at,
            }

    def key(self, project_id: str, repository: str, branch: str, actor: str):
        from rcp.storage.paper_git_links import PaperGitLink

        remote_url(repository)
        directory = self.directory(project_id)
        with self.lock(project_id), self.admission(project_id):
            if branch:
                _git(directory, ["check-ref-format", "refs/heads/" + branch])
            existing = self.store.paper_git_link(project_id)
            if existing and existing.state == "pending":
                if (existing.repository, existing.branch) != (repository, branch):
                    raise GitError("Unlink the pending repository before changing it")
                return self.status(project_id)
            if existing:
                raise GitError("Unlink first")
            own = directory / "id_ed25519"
            if own.is_symlink() or own.exists():
                self._unlink(project_id)
            shared = self.shared_key(project_id, repository)
            if shared is None:
                fingerprint = generate_key(directory, project_id)
            else:
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                own.symlink_to(shared[1])
                fingerprint = ""
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
        if shared is not None:
            return self.link(project_id)
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
                _LOG.warning("Paper Git link of %s dropped: its key is missing", link.project_id)
                with self.lock(link.project_id):
                    self._unlink(link.project_id)
        for directory in self.root.iterdir() if self.root.is_dir() else ():
            if directory.is_symlink() or not directory.is_dir():
                continue
            with self.lock(directory.name):
                if self.store.paper_git_link(directory.name) is None:
                    shutil.rmtree(directory)

    def _remote_heads(self, link) -> tuple[dict[str, str], str]:
        """The remote's branch heads and its default branch, "" when HEAD is unborn."""
        output = _git(
            self.directory(link.project_id),
            ["ls-remote", "--symref", remote_url(link.repository), "HEAD", "refs/heads/*"],
        )
        heads: dict[str, str] = {}
        default = ""
        for line in output.decode(errors="replace").splitlines():
            value, _, ref = line.partition("\t")
            if ref == "HEAD" and value.startswith("ref: refs/heads/"):
                default = value.removeprefix("ref: refs/heads/")
            elif ref.startswith("refs/heads/"):
                heads[ref.removeprefix("refs/heads/")] = value
        return heads, default

    def _head(self, link) -> str:
        return self._remote_heads(link)[0].get(link.branch, "")

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
                if not link.branch:
                    link.branch = self._remote_heads(link)[1] or "main"
                head = self._fetch(link)
                with self.admission(project_id):
                    self._merge(link, head)
                    link.state = "linked"
                    self.store.save_paper_git_link(link)
            except Exception as exc:
                self._error(project_id, exc)
                raise
            return self.status(project_id)

    def _linked(self, project_id: str):
        link = self._record(project_id)
        if link.state != "linked":
            raise GitError("Paper link is pending")
        return link

    def branches(self, project_id: str) -> list[str]:
        with self.lock(project_id):
            return sorted(self._remote_heads(self._linked(project_id))[0])

    def switch(self, project_id: str, branch: str, *, create: bool):
        with self.lock(project_id):
            link = self._linked(project_id)
            directory = self.directory(project_id)
            _git(directory, ["check-ref-format", "refs/heads/" + branch])
            if branch == link.branch and not create:
                return self.status(project_id)
            link.branch = branch
            head = self._head(link)
            if create:
                if head:
                    raise GitError("That branch already exists on GitHub")
                if link.synced_commit:
                    _git(
                        directory,
                        [
                            "push",
                            f"--force-with-lease=refs/heads/{branch}:",
                            remote_url(link.repository),
                            link.synced_commit + ":refs/heads/" + branch,
                        ],
                    )
                with self.admission(project_id):
                    self._checked(link)
                return self.status(project_id)
            if not head:
                raise GitError("Paper Git branch is missing from the remote")
            head = self._fetch(link)
            with self.admission(project_id):
                self.store.save_paper_git_link(link)
                try:
                    self._merge(link, head)
                except Exception as exc:
                    self._error(project_id, exc)
                    raise
            return self.status(project_id)

    def _error(self, project_id: str, error: Exception) -> None:
        link = self._record(project_id)
        link.last_error = str(error)
        self.store.save_paper_git_link(link)

    def pull(self, project_id: str):
        with self.lock(project_id):
            link = self._linked(project_id)
            try:
                head = self._fetch(link)
                with self.admission(project_id):
                    self._merge(link, head)
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
        desired = {}
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
        heads = self._remote_heads(link)[0]
        head = heads.get(link.branch, "")
        if not head and link.synced_commit:
            raise GitError("Paper Git branch is missing from the remote")
        if not head and heads:
            names = sorted(heads)
            more = f" and {len(names) - 5} more" if len(names) > 5 else ""
            raise GitError(
                f"Branch {link.branch} is not on GitHub. It has {', '.join(names[:5])}{more}."
            )
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
        base = read_tree(self.directory(link.project_id), link.synced_commit)
        for item in link.skipped:
            base.pop(item["path"], None)
        return base

    def _checked(self, link) -> None:
        link.last_error = ""
        link.checked_at = self.store.now()
        self.store.save_paper_git_link(link)

    def _merge(self, link, head: str) -> None:
        """Apply GitHub's head as a multi-file write; keystrokes outside its changes survive."""
        from rcp.paper.git_merge import merge_trees

        if head == link.synced_commit:
            self._checked(link)
            return
        theirs = read_tree(self.directory(link.project_id), head)
        before = self.live.run_sync(self.live.snapshot(link.project_id))
        merged = merge_trees(self._base(link), theirs, snapshot_tree(before))
        if merged.ops:
            self.live.run_sync(
                self.live.apply(link.project_id, merged.ops, "github", checkpoint=True)
            )
        after = self.live.run_sync(self.live.snapshot(link.project_id))
        link.held = []
        link.skipped = merged.skipped
        link.synced_commit = head
        link.synced_revision = after.revision
        self._checked(link)
        self._pin(link)

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

    @contextmanager
    def _diff_range(self, project_id: str):
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
                yield (
                    link,
                    tree,
                    lambda argv, paths=(): _git(
                        directory,
                        ["diff", "--no-renames", *argv, base, target, "--", *paths],
                        environment={**env, "GIT_LITERAL_PATHSPECS": "1"},
                    ),
                )

    def changed_files(self, project_id: str):
        with self._diff_range(project_id) as (link, tree, diff):
            output = diff(["--numstat", "-z"])
            remote = read_tree(self.directory(project_id), link.synced_commit)
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

    def file_diff(self, project_id: str, path: str):
        with self._diff_range(project_id) as (_, _, diff):
            output = diff(["--no-color", "--no-ext-diff"], [path])
        lines = output.decode("utf-8", errors="replace").splitlines()
        binary = any(line.startswith("Binary files ") for line in lines)
        body = list(dropwhile(lambda line: not line.startswith("@@"), lines))
        return {
            "lines": body[:PAPER_GIT_DIFF_LINES],
            "more": max(0, len(body) - PAPER_GIT_DIFF_LINES),
            "binary": binary,
        }

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
                    link = self._record(project_id)
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
