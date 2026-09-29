"""Task stage roots on one execution account: create, fill, sweep, and remove them.

Standard library only: `RemoteRunStage` ships this module's source over SSH.
New stages live in the account's `~/.rcp/stages`, never `/tmp`.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from contextlib import suppress

#: The only stage names RCP makes (`run_stage._safe_label` or `mkdtemp`);
#: `rcp.terminals.profile` repeats this rule; a test lists the same folders through both.
STAGE_NAME = re.compile(r"rcp-run\.[A-Za-z0-9._-]+")
#: Where stages lived before `~/.rcp/stages`; a later release drops it.
LEGACY_PARENT = "/tmp"


def create_stage(label: str, reuse: bool) -> str:
    """The stage root for `label` (a fresh one when empty); refuses an unsafe parent."""

    rcp = os.path.join(os.path.expanduser("~"), ".rcp")
    base = os.path.join(rcp, "stages")
    for directory in (rcp, base):
        with suppress(FileExistsError):
            os.mkdir(directory, 0o700)
    # A parent another user can write could swap a retained stage, so both must be ours.
    for directory in (rcp, base):
        info = os.lstat(directory)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError("remote stage directory is unsafe")
        if info.st_mode & 0o022:
            # Ours but made under a 0002 umask, as `rcp.rcp_home` also repairs.
            os.chmod(directory, stat.S_IMODE(info.st_mode) & ~0o022)
    if not label:
        return tempfile.mkdtemp(prefix="rcp-run.", dir=base)
    root = os.path.join(base, "rcp-run." + label)
    legacy = os.path.join(LEGACY_PARENT, "rcp-run." + label)
    # Legacy: a reused stage made before RCP left /tmp stays there; a later release removes this.
    if reuse and not os.path.lexists(root) and os.path.lexists(legacy):
        return legacy
    try:
        os.mkdir(root, 0o700)
    except FileExistsError:
        if not reuse:
            raise ValueError("remote run stage already exists") from None
    return root


def legacy_stage_roots() -> list[str]:
    """Stages left in `/tmp` from before `~/.rcp`; symlinks and stray names skipped.

    Only names RCP made count: a stray one could hold text no mount can carry.
    """

    return sorted(
        {
            os.path.realpath(path)
            for path in glob.glob(os.path.join(LEGACY_PARENT, "rcp-run.*"))
            if STAGE_NAME.fullmatch(os.path.basename(path))
            and os.path.isdir(path)
            and not os.path.islink(path)
        }
    )


def make_writable(path: str) -> None:
    if os.path.islink(path):
        return
    os.chmod(path, 0o700)
    if not os.path.isdir(path):
        return
    with os.scandir(path) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                make_writable(entry.path)
            elif not entry.is_symlink():
                os.chmod(entry.path, 0o600)


def remove_tree(path: str) -> None:
    if not os.path.lexists(path):
        return
    if os.path.islink(path) or not os.path.isdir(path):
        os.unlink(path)
        return
    make_writable(path)
    shutil.rmtree(path)


def sweep_stages(retain_days: int, protected: set[str]) -> None:
    """Remove stages older than `retain_days`, in `~/.rcp/stages` and legacy `/tmp`.

    Best effort: a stage that cannot be removed stays for the next sweep.
    """

    cutoff = time.time() - retain_days * 86400
    base = os.path.join(os.path.expanduser("~"), ".rcp", "stages")
    for parent in (base, LEGACY_PARENT):
        for target in glob.glob(os.path.join(parent, "rcp-run.*")):
            if target in protected or not STAGE_NAME.fullmatch(os.path.basename(target)):
                continue
            with suppress(OSError):
                if os.path.isdir(target) and os.path.getmtime(target) < cutoff:
                    remove_tree(target)


def check_stage(root: str) -> None:
    """Exit 0 when `root` is this account's own private stage; 1 when it is not, 2 when unknown.

    The saved root itself is checked, never followed through a replacement.
    """

    base = os.path.join(os.path.expanduser("~"), ".rcp", "stages")
    legacy = os.path.dirname(root) == LEGACY_PARENT
    if not STAGE_NAME.fullmatch(os.path.basename(root)) or (
        os.path.dirname(root) != base and not legacy
    ):
        print("remote run stage is outside this account", file=sys.stderr)
        raise SystemExit(1)
    try:
        info = os.lstat(root)
    except (FileNotFoundError, NotADirectoryError):
        raise SystemExit(1) from None
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
    for unsafe, message in (
        (not stat.S_ISDIR(info.st_mode), "remote run stage is not a directory"),
        (info.st_uid != os.geteuid(), "remote run stage has the wrong owner"),
        (stat.S_IMODE(info.st_mode) != 0o700, "remote run stage has unsafe permissions"),
    ):
        if unsafe:
            print(message, file=sys.stderr)
            raise SystemExit(1)


def remove_stage(root: str) -> None:
    remove_tree(root)
    if os.path.lexists(root):
        raise SystemExit(1)


def prepare_artifacts(workspace: str, scope: str, reuse: bool) -> None:
    """Create (or with `reuse` adopt) one turn's artifact folder without following links."""

    if os.path.islink(workspace) or not os.path.isdir(workspace):
        raise SystemExit("workspace is unavailable")
    turns = os.path.join(workspace, "turns")
    if os.path.lexists(turns) and (os.path.islink(turns) or not os.path.isdir(turns)):
        raise SystemExit("artifact parent is unsafe")
    os.makedirs(turns, mode=0o700, exist_ok=True)
    scope_path = os.path.join(turns, scope)
    target = os.path.join(scope_path, "artifacts")
    if reuse:
        if (
            os.path.islink(scope_path)
            or not os.path.isdir(scope_path)
            or os.path.islink(target)
            or not os.path.isdir(target)
        ):
            raise SystemExit("saved artifact directory is unavailable")
    else:
        remove_tree(scope_path)
        os.makedirs(target, mode=0o700, exist_ok=False)


def _fingerprint(path: str, immutable: bool = False) -> tuple[str, object]:
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise ValueError("staged input contains a symlink")
    if immutable and info.st_mode & 0o222:
        raise ValueError("reusable staged input is writable")
    if stat.S_ISDIR(info.st_mode):
        children = []
        with os.scandir(path) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                children.append((entry.name, _fingerprint(entry.path, immutable)))
        return ("directory", children)
    if stat.S_ISREG(info.st_mode):
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return ("file", digest.hexdigest())
    raise ValueError("staged input is not a regular file or directory")


def _protect(path: str) -> None:
    info = os.lstat(path)
    if stat.S_ISDIR(info.st_mode):
        with os.scandir(path) as entries:
            for entry in entries:
                _protect(entry.path)
        os.chmod(path, 0o500)
    elif stat.S_ISREG(info.st_mode):
        os.chmod(path, 0o400)


def commit_inputs(
    root: str, batch: str, labels: list[str], transferred: bool, reusable: set[str]
) -> None:
    """Move one transferred input batch into the stage's immutable `inputs`, or none of it.

    Exit 44 when the transfer failed, after removing the partial batch.
    """

    root, batch = os.path.abspath(root), os.path.abspath(batch)
    inputs = os.path.join(root, "inputs")
    if not transferred:
        remove_tree(batch)
        raise SystemExit(44)
    if not reusable.issubset(labels):
        remove_tree(batch)
        raise ValueError("reusable input labels are invalid")
    entries = []
    moved = []
    try:
        if os.path.dirname(batch) != root or not os.path.basename(batch).startswith(
            ".input-batch-"
        ):
            raise ValueError("remote input batch is outside its stage")
        if os.path.islink(root) or not os.path.isdir(root):
            raise ValueError("run stage is unavailable")
        if os.path.islink(inputs) or not os.path.isdir(inputs):
            raise ValueError("input root is unavailable")
        if sorted(os.listdir(batch)) != labels:
            raise ValueError("remote input batch is incomplete")
        for label in labels:
            if label != os.path.basename(label) or label in ("", ".", ".."):
                raise ValueError("remote input label is unsafe")
            source = os.path.join(batch, label)
            target = os.path.join(inputs, label)
            source_fingerprint = _fingerprint(source)
            if os.path.lexists(target):
                if label not in reusable:
                    raise FileExistsError(target)
                if _fingerprint(target, True) != source_fingerprint:
                    raise ValueError("reusable staged input does not match its content label")
            else:
                entries.append((source, target))
        for source, target in entries:
            os.replace(source, target)
            moved.append((source, target))
        for _source, target in moved:
            _protect(target)
        remove_tree(batch)
    except BaseException:
        for source, target in reversed(moved):
            if os.path.lexists(target) and not os.path.lexists(source):
                make_writable(target)
                os.replace(target, source)
        remove_tree(batch)
        raise


def main(argv: list[str]) -> int:
    try:
        if argv[1:2] == ["legacy"]:
            print(json.dumps(legacy_stage_roots()))
        elif len(argv) == 4 and argv[1] == "sweep":
            sweep_stages(int(argv[2]), set(json.loads(argv[3])))
        elif len(argv) == 3 and argv[1] == "check":
            check_stage(argv[2])
        elif len(argv) == 3 and argv[1] == "remove":
            remove_stage(argv[2])
        elif len(argv) == 5 and argv[1] == "prepare-artifacts":
            prepare_artifacts(argv[2], argv[3], argv[4] == "1")
        elif len(argv) == 7 and argv[1] == "commit-inputs":
            commit_inputs(
                argv[2], argv[3], json.loads(argv[4]), argv[5] == "1", set(json.loads(argv[6]))
            )
        elif len(argv) == 4 and argv[1] == "create":
            print(create_stage(argv[2], argv[3] == "1"))
        else:
            print("remote stage request is invalid", file=sys.stderr)
            return 2
    except (OSError, ValueError) as exc:
        print(str(exc)[:600], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through SSH source execution
    raise SystemExit(main(sys.argv))
