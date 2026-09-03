from __future__ import annotations

import pytest

import rcp
from rcp.build_identity import BuildIdentity, build_identity


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        (
            "0.3.2",
            BuildIdentity(
                version="0.3.2",
                base_version="0.3.2",
                build=None,
                commit=None,
            ),
        ),
        (
            "0.3.2+build.412.gfe06636",
            BuildIdentity(
                version="0.3.2+build.412.gfe06636",
                base_version="0.3.2",
                build=412,
                commit="fe06636",
            ),
        ),
        (
            "0.3.2+build.latest.gfe06636",
            BuildIdentity(
                version="0.3.2+build.latest.gfe06636",
                base_version="0.3.2",
                build=None,
                commit=None,
            ),
        ),
    ],
)
def test_build_identity_parses_package_version(
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    expected: BuildIdentity,
) -> None:
    monkeypatch.setattr(rcp, "__version__", version)

    assert build_identity() == expected
