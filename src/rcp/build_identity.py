"""Build identity derived from the package version."""

from __future__ import annotations

import re
from dataclasses import dataclass

import rcp

_BUILD_LOCAL_VERSION = re.compile(r"build\.(\d+)\.g([0-9a-fA-F]{7,40})")


@dataclass(frozen=True)
class BuildIdentity:
    """Version facts embedded in one RCP build."""

    version: str
    base_version: str
    build: int | None
    commit: str | None


def build_identity() -> BuildIdentity:
    """Parse the build metadata encoded in ``rcp.__version__``."""

    version = rcp.__version__
    base_version, separator, local_version = version.partition("+")
    match = _BUILD_LOCAL_VERSION.fullmatch(local_version) if separator else None
    if match is None:
        return BuildIdentity(
            version=version,
            base_version=base_version,
            build=None,
            commit=None,
        )
    return BuildIdentity(
        version=version,
        base_version=base_version,
        build=int(match.group(1)),
        commit=match.group(2),
    )


__all__ = ["BuildIdentity", "build_identity"]
