"""One stdlib-only Git transfer implementation, also shipped over SSH."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path


class RepositoryGit:
    """Bounded Git operations with repository hooks and network access disabled."""

    def __init__(self, path: Path, deadline: float, output_limit: int) -> None:
        self.path = path
        self.deadline = deadline
        self.output_limit = output_limit

    def git(self, *arguments: str, allow_missing: bool = False) -> bytes:
        environment = {
            "PATH": os.defpath,
            "HOME": str(Path.home()),
            "TMPDIR": tempfile.gettempdir(),
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1",
        }
        command = ["git", "-C", str(self.path)]
        for setting in (
            "core.hooksPath=/dev/null",
            "core.fsmonitor=false",
            "core.untrackedCache=false",
            "submodule.recurse=false",
            "protocol.allow=never",
            "core.protectHFS=true",
            "core.protectNTFS=true",
            "gc.auto=0",
            "maintenance.auto=false",
        ):
            command.extend(("-c", setting))
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("repository Git transfer timed out")
            result = subprocess.run(
                [*command, *arguments],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=errors,
                env=environment,
                timeout=remaining,
                check=False,
            )
            output.seek(0)
            errors.seek(0)
            content = output.read(self.output_limit + 1)
            diagnostic = errors.read(self.output_limit + 1)
        if max(len(content), len(diagnostic)) > self.output_limit:
            raise ValueError("repository Git transfer inventory exceeded its limit")
        if result.returncode != 0 and not (
            allow_missing and result.returncode == 1 and not content and not diagnostic
        ):
            raise ValueError(f"repository Git transfer could not {arguments[0]}")
        return content

    def require_checkout(self) -> str:
        path = self.path
        if (
            not path.is_absolute()
            or path == Path("/")
            or ".." in path.parts
            or path.resolve() != path
            or not path.is_dir()
        ):
            raise ValueError("repository transfer requires an exact nonsymlink checkout root")
        metadata = path / ".git"
        if metadata.is_symlink() or not metadata.is_dir():
            raise ValueError("repository transfer requires ordinary directory Git metadata")
        for root, directories, files in os.walk(metadata, followlinks=False):
            if time.monotonic() >= self.deadline:
                raise ValueError("repository Git transfer timed out")
            for name in [*directories, *files]:
                mode = (Path(root) / name).lstat().st_mode
                if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                    raise ValueError("repository transfer refuses unsafe Git metadata")
        for name in (
            "MERGE_HEAD",
            "CHERRY_PICK_HEAD",
            "REVERT_HEAD",
            "BISECT_START",
            "rebase-apply",
            "rebase-merge",
            "sequencer",
            "index.lock",
            "HEAD.lock",
            "shallow",
            "info/grafts",
            "objects/info/alternates",
            "refs/replace",
        ):
            if (metadata / name).exists():
                raise ValueError("repository transfer refuses unfinished or indirect Git state")
        unsafe = self.git(
            "config",
            "--local",
            "--no-includes",
            "--name-only",
            "--get-regexp",
            r"^(include(\..*)?|includeif\..*|filter\..*|extensions\..*|"
            r"core\.(worktree|sparsecheckout)|remote\..*\.promisor)$",
            allow_missing=True,
        )
        if unsafe:
            raise ValueError("repository transfer refuses filters or indirect Git configuration")
        if self.git("rev-parse", "--show-toplevel").rstrip(b"\n") != os.fsencode(path):
            raise ValueError("repository transfer requires the exact Git checkout root")
        head = self.revision()
        self.require_tree(head)
        return head

    def revision(self) -> str:
        head = self.git("rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
        if re.fullmatch(r"[0-9a-f]{40}", head) is None:
            raise ValueError("repository transfer requires a full SHA-1 commit")
        return head

    def require_tree(self, head: str) -> None:
        for entry in self.git("ls-tree", "-rz", "--full-tree", head).split(b"\0"):
            if not entry:
                continue
            metadata, name = entry.split(b"\t", 1)
            mode, kind, _object_id = metadata.split(b" ", 2)
            if name.split(b"/", 1)[0].lower() in {b".research", b".recovery"}:
                raise ValueError("repository transfer refuses tracked .research or .recovery")
            if kind != b"blob" or mode not in {b"100644", b"100755", b"120000"}:
                raise ValueError("repository transfer cannot carry submodule contents")
            if name.rsplit(b"/", 1)[-1] == b".gitattributes" and mode != b"120000":
                attributes = self.git("cat-file", "blob", _object_id.decode("ascii"))
                if any(
                    not line.lstrip().startswith(b"#")
                    and re.search(rb"(^|\s)filter(?:=|\s|$)", line)
                    for line in attributes.splitlines()
                ):
                    raise ValueError("repository transfer cannot carry LFS or filtered contents")

    def require_clean(self, *, allow_untracked: bool = False) -> None:
        for entry in self.git("ls-files", "-v", "-z").split(b"\0"):
            if entry and entry[:1] != b"H":
                raise ValueError("repository transfer refuses hidden or conflicted index entries")
        changes = self.git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored")
        for entry in changes.split(b"\0"):
            if not entry:
                continue
            status, path = entry[:2], entry[3:]
            if status in {b"??", b"!!"} and (allow_untracked or path.startswith(b".research/")):
                continue
            raise ValueError("repository transfer target has uncommitted or untracked work")
        research = self.path / ".research"
        if research.is_symlink() or (research.exists() and not research.is_dir()):
            raise ValueError("repository transfer target .research is not a safe directory")


def _validated_bundle(
    git: RepositoryGit, bundle: Path, expected_head: str, temporary: Path
) -> None:
    inspection = RepositoryGit(temporary, git.deadline, git.output_limit)
    inspection.git("init", "--bare", "--template=")
    if inspection.git("bundle", "list-heads", str(bundle)).splitlines() != [
        f"{expected_head} HEAD".encode("ascii")
    ]:
        raise ValueError("repository transfer bundle does not contain exactly the reviewed HEAD")
    # Verification in an empty repository rejects bundles with prerequisites.
    inspection.git("bundle", "verify", str(bundle))
    inspection.git("bundle", "unbundle", str(bundle))
    if inspection.git("cat-file", "-t", expected_head).strip() != b"commit":
        raise ValueError("repository transfer bundle HEAD is not a commit")
    inspection.git("fsck", "--strict", "--no-reflogs", "--no-dangling", expected_head)
    inspection.require_tree(expected_head)


def main(argv: list[str]) -> int:
    if len(argv) != 7 or argv[1] not in {"probe", "capture", "install"}:
        return 2
    try:
        operation, path, expected_head = argv[1:4]
        timeout, output_limit, copy_buffer = float(argv[4]), int(argv[5]), int(argv[6])
        if operation != "probe" and re.fullmatch(r"[0-9a-f]{40}", expected_head) is None:
            raise ValueError("repository transfer requires one full Git commit")
        repository = RepositoryGit(Path(path), time.monotonic() + timeout, output_limit)
        initial_head = repository.require_checkout()
        if operation == "probe":
            print(initial_head)
            return 0
        if operation == "capture" and initial_head != expected_head:
            raise ValueError("source repository HEAD changed after transfer review")
        if operation == "install":
            repository.require_clean(allow_untracked=initial_head == expected_head)
        with tempfile.TemporaryDirectory(prefix="rcp-transfer-git-") as temporary_name:
            temporary = Path(temporary_name)
            bundle = temporary / "head.bundle"
            if operation == "capture":
                repository.git("bundle", "create", "--version=2", str(bundle), "HEAD")
            else:
                with bundle.open("xb") as output:
                    shutil.copyfileobj(sys.stdin.buffer, output, length=copy_buffer)
            inspection = temporary / "inspection"
            inspection.mkdir()
            _validated_bundle(repository, bundle, expected_head, inspection)
            if repository.require_checkout() != initial_head:
                raise ValueError("repository HEAD changed during transfer")
            if operation == "capture":
                with bundle.open("rb") as source:
                    shutil.copyfileobj(source, sys.stdout.buffer, length=copy_buffer)
                if repository.revision() != expected_head:
                    raise ValueError("source repository HEAD changed during transfer")
            elif initial_head == expected_head:
                # Import may have published kept artifacts before a later step
                # failed. The exact revision needs no Git mutation on retry,
                # so untracked files can remain without any overwrite risk.
                repository.require_clean(allow_untracked=True)
                if repository.revision() != expected_head:
                    raise ValueError("target repository HEAD changed during transfer")
                print(expected_head)
            else:
                repository.require_clean()
                repository.git("bundle", "unbundle", str(bundle))
                if repository.revision() != initial_head:
                    raise ValueError("target repository HEAD changed during transfer")
                repository.require_clean()
                repository.git("checkout", "--detach", "--no-overwrite-ignore", expected_head)
                if repository.revision() != expected_head:
                    raise ValueError("target repository did not reach the reviewed HEAD")
                repository.require_clean()
                print(expected_head)
        return 0
    except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "repository Git transfer failed"
        print(message, file=sys.stderr)
        return 3


if __name__ == "__main__":  # pragma: no cover - executed as shipped source
    raise SystemExit(main(sys.argv))
