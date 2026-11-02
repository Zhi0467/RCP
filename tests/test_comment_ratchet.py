from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "comment_ratchet.py"
_SPEC = importlib.util.spec_from_file_location("comment_ratchet", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
ratchet = importlib.util.module_from_spec(_SPEC)
sys.modules["comment_ratchet"] = ratchet
_SPEC.loader.exec_module(ratchet)


def test_each_banned_comment_line_counts_once() -> None:
    source = (
        "x = 1  # trailing\n"
        "# first line of a block\n"
        "# second line of a block\n"
        "def f():\n"
        '    """A docstring # with a hash is not a comment."""\n'
        '    return "# nor is a string"\n'
    )

    assert ratchet.count_comments(source) == 3


def test_allowed_comments_are_not_counted() -> None:
    source = (
        "#!/usr/bin/env python3\n"
        "# -*- coding: utf-8 -*-\n"
        "# Invariant 10f: the watermark moves only on ingestion.\n"
        "# see invariant 3b\n"
        "import os  # noqa: F401\n"
        "y: int = os.sep  # type: ignore[assignment]\n"
        "z = 1  # pyright: ignore[reportGeneralTypeIssues]\n"
        "w = 2  # basedpyright: ignore\n"
        "# fmt: off\n"
    )

    assert ratchet.count_comments(source) == 0


def test_shebang_is_allowed_only_on_the_first_line() -> None:
    assert ratchet.count_comments("x = 1\n#!/usr/bin/env python3\n") == 1


def test_a_rise_fails_and_names_the_file() -> None:
    baseline = {"a.py": {"comment": 2}}
    current = {"a.py": {"comment": 3}, "b.py": {"comment": 1}}

    assert {(item.path, item.count, item.allowed) for item in ratchet.rises(current, baseline)} == {
        ("a.py", 3, 2),
        ("b.py", 1, 0),
    }


def test_falls_and_deleted_files_shrink_the_baseline() -> None:
    baseline = {"a.py": {"comment": 2}, "gone.py": {"comment": 4}, "c.py": {"comment": 1}}
    current = {"a.py": {"comment": 1}, "c.py": {"comment": 0}}

    assert ratchet.rises(current, baseline) == []
    assert json.loads(ratchet.render(current)) == {"a.py": {"comment": 1}}


def test_baseline_puts_one_sorted_file_per_line() -> None:
    text = ratchet.render({"b.py": {"comment": 2}, "a.py": {"comment": 1}})

    lines = text.splitlines()
    assert lines[1].startswith('  "a.py"')
    assert lines[2].startswith('  "b.py"')
    assert json.loads(text) == {"a.py": {"comment": 1}, "b.py": {"comment": 2}}
