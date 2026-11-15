"""The restore schema registry's test-side contract.

`SUPPORTED_RESTORE_DATABASE_SCHEMAS` is derived: main's block, then every
digest the tests look up that main does not have. Tests look digests up
through `assert_restore_schema_registered`, and
`scripts/regenerate_registries.py` runs them with `RECORD_VARIABLE` set to
collect the lookups instead of checking them.
"""

from __future__ import annotations

import os
import re

from rcp.server_ops import restore

COMMAND = "uv run python scripts/regenerate_registries.py"
RECORD_VARIABLE = "RCP_RESTORE_SCHEMA_RECORD"
_BLOCK = re.compile(
    r"(?<=^SUPPORTED_RESTORE_DATABASE_SCHEMAS = frozenset\(\n    \{\n).*?(?=^    \}\n\))",
    re.DOTALL | re.MULTILINE,
)
_DIGEST = re.compile(r'"([0-9a-f]{64})"')


def assert_restore_schema_registered(digest: str) -> None:
    record = os.environ.get(RECORD_VARIABLE)
    if record:
        with open(record, "a", encoding="utf-8") as lookups:
            lookups.write(f"{digest}\n")
        return
    assert digest in restore.SUPPORTED_RESTORE_DATABASE_SCHEMAS, (
        f"Restore does not accept schema {digest}. Run `{COMMAND}`."
    )


def _block(text: str) -> re.Match[str]:
    blocks = list(_BLOCK.finditer(text))
    if len(blocks) != 1:
        raise ValueError(f"expected one restore schema block, found {len(blocks)}")
    return blocks[0]


def block_digests(text: str) -> set[str]:
    return set(_DIGEST.findall(_block(text).group()))


def splice_restore_block(working_text: str, base_text: str, observed: set[str]) -> str:
    """Main's block byte-for-byte, then the observed digests main lacks."""

    base = _block(base_text).group()
    new = sorted(observed - set(_DIGEST.findall(base)))
    working = _block(working_text)
    lines = "".join(f'        "{digest}",\n' for digest in new)
    return working_text[: working.start()] + base + lines + working_text[working.end() :]


def frozen_block_is_kept(working_text: str, base_text: str) -> bool:
    return _block(working_text).group().startswith(_block(base_text).group())
