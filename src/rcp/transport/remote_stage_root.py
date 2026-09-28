"""Create, sweep, or list the legacy `/tmp` task stage roots on one execution account.

Standard library only: `RemoteRunStage` ships this module's source over SSH.
New stages live in the account's `~/.rcp/stages`, never `/tmp`.
"""

from __future__ import annotations

import glob
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
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ValueError("remote stage directory is unsafe")
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


def main(argv: list[str]) -> int:
    try:
        if argv[1:2] == ["legacy"]:
            print(json.dumps(legacy_stage_roots()))
        elif len(argv) == 4 and argv[1] == "sweep":
            sweep_stages(int(argv[2]), set(json.loads(argv[3])))
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
