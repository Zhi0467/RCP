from __future__ import annotations

from pathlib import Path

import pytest

from rcp.server_ops import restore
from tests.restore_schemas import (
    RECORD_VARIABLE,
    assert_restore_schema_registered,
    block_digests,
    frozen_block_is_kept,
    splice_restore_block,
)
from tests.shipped_sources import ROOT, rewrite_py39_block

A, B, C, D = (letter * 64 for letter in "abcd")


def _source(*lines: str) -> str:
    body = "".join(f"{line}\n" for line in lines)
    return (
        f"X = 1\nSUPPORTED_RESTORE_DATABASE_SCHEMAS = frozenset(\n    {{\n{body}    }}\n)\nY = 2\n"
    )


def _entry(digest: str) -> str:
    return f'        "{digest}",'


def test_splice_keeps_the_base_block_and_appends_sorted_new_digests() -> None:
    base = _source(_entry(B), "        # why", _entry(A))
    working = _source(_entry(B), "        # why", _entry(A), _entry(C)).replace("Y = 2", "Y = 3")

    spliced = splice_restore_block(working, base, {D, C, A})

    assert spliced == _source(_entry(B), "        # why", _entry(A), _entry(C), _entry(D)).replace(
        "Y = 2", "Y = 3"
    )
    assert splice_restore_block(spliced, base, {D, C, A}) == spliced
    assert frozen_block_is_kept(spliced, base)


def test_splice_drops_a_branch_digest_no_test_looks_up() -> None:
    base = _source(_entry(A))

    spliced = splice_restore_block(_source(_entry(A), _entry(C)), base, {D})

    assert block_digests(spliced) == {A, D}


def test_a_removed_or_reordered_base_digest_breaks_the_frozen_block() -> None:
    base = _source(_entry(A), _entry(B))

    assert not frozen_block_is_kept(_source(_entry(A), _entry(C)), base)
    assert not frozen_block_is_kept(_source(_entry(B), _entry(A)), base)


@pytest.mark.parametrize("text", ["X = 1\n", _source(_entry(A)) * 2])
def test_splice_refuses_a_text_without_exactly_one_block(text: str) -> None:
    with pytest.raises(ValueError, match="one restore schema block"):
        splice_restore_block(text, _source(_entry(A)), set())


def test_the_real_registry_block_is_found_whole() -> None:
    source = Path(restore.__file__).read_text(encoding="utf-8")

    assert block_digests(source) == restore.SUPPORTED_RESTORE_DATABASE_SCHEMAS
    assert splice_restore_block(source, source, set()) == source


def test_lookup_checks_the_registry_unless_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(RECORD_VARIABLE, raising=False)
    assert_restore_schema_registered(next(iter(restore.SUPPORTED_RESTORE_DATABASE_SCHEMAS)))
    with pytest.raises(AssertionError, match="regenerate_registries"):
        assert_restore_schema_registered(A)

    record = tmp_path / "lookups"
    monkeypatch.setenv(RECORD_VARIABLE, str(record))
    assert_restore_schema_registered(A)
    assert_restore_schema_registered(B)

    assert record.read_text(encoding="utf-8").split() == [A, B]


def test_py39_rewrite_replaces_the_block_with_sorted_exact_paths() -> None:
    text = (
        "[tool.other]\n"
        '"keep" = "py39ish"\n'
        "[tool.ruff.per-file-target-version]\n"
        '"src/rcp/gone.py" = "py39"\n'
        '"src/rcp/*/glob.py" = "py39"\n'
        "\n[tool.ruff.lint]\n"
    )

    rewritten = rewrite_py39_block(text, ["src/rcp/b.py", "src/rcp/a.py"])

    assert rewritten == (
        "[tool.other]\n"
        '"keep" = "py39ish"\n'
        "[tool.ruff.per-file-target-version]\n"
        '"src/rcp/a.py" = "py39"\n'
        '"src/rcp/b.py" = "py39"\n'
        "\n[tool.ruff.lint]\n"
    )


def test_py39_rewrite_refuses_a_file_without_exactly_one_block() -> None:
    with pytest.raises(ValueError, match="one py39 block"):
        rewrite_py39_block("[tool.ruff]\n", ["src/rcp/a.py"])


def test_the_real_py39_block_is_found() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert '"src/rcp/only.py" = "py39"' in rewrite_py39_block(text, ["src/rcp/only.py"])
