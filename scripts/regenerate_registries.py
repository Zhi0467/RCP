"""Rewrite the registries derived from the schema and the shipped-source scan.

    uv run python scripts/regenerate_registries.py
    uv run python scripts/regenerate_registries.py --check-frozen <main ref>

The first form writes the `py39` lines of `pyproject.toml`, then runs every
test that looks a restore schema up, in recording mode, and writes
`SUPPORTED_RESTORE_DATABASE_SCHEMAS` as main's block followed by the digests
this branch adds. The second writes and fetches nothing: it fails when the block
no longer starts with the block at that ref.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.restore_schemas import (  # noqa: E402
    COMMAND,
    RECORD_VARIABLE,
    block_digests,
    frozen_block_is_kept,
    splice_restore_block,
)
from tests.shipped_sources import (  # noqa: E402
    py39_targets,
    rewrite_py39_block,
    shipped_relative_paths,
)

RESTORE = "src/rcp/server_ops/restore.py"
PYPROJECT = "pyproject.toml"
LOOKUP = "assert_restore_schema_registered"


def _write(relative: str, text: str) -> None:
    path = ROOT / relative
    changed = path.read_text(encoding="utf-8") != text
    if changed:
        path.write_text(text, encoding="utf-8")
    print(f"{'updated' if changed else 'unchanged'} {relative}")


def _report(sign: str, before: set[str], after: set[str]) -> None:
    for item in sorted(after - before):
        print(f"  {sign} {item}")


def _git(*arguments: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(ROOT), *arguments], capture_output=True, text=True, check=False
    )
    if done.returncode:
        raise SystemExit(f"git {' '.join(arguments)} failed: {done.stderr.strip()}")
    return done.stdout


def _main_restore_source() -> str:
    _git("fetch", "origin", "main")
    return _git("show", f"FETCH_HEAD:{RESTORE}")


def _regenerate_py39() -> None:
    text = (ROOT / PYPROJECT).read_text(encoding="utf-8")
    shipped = shipped_relative_paths()
    before = set(py39_targets(text))
    _report("+", before, set(shipped))
    _report("-", set(shipped), before)
    _write(PYPROJECT, rewrite_py39_block(text, shipped))


def _observed_digests() -> set[str]:
    files = sorted(
        str(path.relative_to(ROOT))
        for path in (ROOT / "tests").glob("test_*.py")
        if LOOKUP in path.read_text(encoding="utf-8")
    )
    with tempfile.TemporaryDirectory() as folder:
        record = Path(folder) / "lookups"
        record.touch()
        done = subprocess.run(
            [sys.executable, "-m", "pytest", *files],
            cwd=ROOT,
            env={**os.environ, RECORD_VARIABLE: str(record)},
            check=False,
        )
        observed = set(record.read_text(encoding="utf-8").split())
    if done.returncode:
        raise SystemExit(f"The restore schema tests failed; {RESTORE} was not written.")
    if not observed:
        raise SystemExit(
            f"The restore schema tests looked no digest up; {RESTORE} was not written."
        )
    return observed


def _regenerate_restore() -> None:
    base = _main_restore_source()
    observed = _observed_digests()
    working = (ROOT / RESTORE).read_text(encoding="utf-8")
    spliced = splice_restore_block(working, base, observed)
    print(f"SQLite {sqlite3.sqlite_version}")
    _report("+", block_digests(working), block_digests(spliced))
    _report("-", block_digests(spliced), block_digests(working))
    _write(RESTORE, spliced)


def _check_frozen(main: str) -> None:
    working = (ROOT / RESTORE).read_text(encoding="utf-8")
    if not frozen_block_is_kept(working, _git("show", f"{main}:{RESTORE}")):
        raise SystemExit(
            f"{RESTORE} no longer starts its schema block with main's. "
            f"Merged digests are frozen. Run `{COMMAND}`."
        )


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check-frozen"] and len(sys.argv) == 3:
        _check_frozen(sys.argv[2])
    elif sys.argv[1:]:
        raise SystemExit(__doc__)
    else:
        _regenerate_py39()
        _regenerate_restore()
