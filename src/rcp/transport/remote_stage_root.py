"""Create a task stage root, or list the legacy `/tmp` ones, on one execution account.

Standard library only: `RemoteRunStage` ships this module's source over SSH.
New stages live in the account's `~/.rcp/stages`, never `/tmp`.
"""

from __future__ import annotations

import glob
import json
import os
import re
import stat
import sys
import tempfile
from contextlib import suppress

#: The only stage names RCP makes; `rcp.terminals.profile` repeats this rule.
LEGACY_STAGE_NAME = re.compile(r"rcp-run\.[A-Za-z0-9_-]+")


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
    legacy = "/tmp/rcp-run." + label
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
            for path in glob.glob("/tmp/rcp-run.*")
            if LEGACY_STAGE_NAME.fullmatch(os.path.basename(path))
            and os.path.isdir(path)
            and not os.path.islink(path)
        }
    )


def main(argv: list[str]) -> int:
    try:
        if argv[1:2] == ["legacy"]:
            print(json.dumps(legacy_stage_roots()))
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
