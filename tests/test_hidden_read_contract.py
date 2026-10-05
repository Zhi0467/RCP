from __future__ import annotations

from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from rcp.core.models import (
    HiddenReadKeyEvidence,
    HiddenReadScope,
    HiddenReadStatus,
    MachineHiddenReadProjection,
)
from rcp.limits import (
    HIDDEN_READ_ENV_MAX_COUNT,
    HIDDEN_READ_ENV_NAME_MAX_LENGTH,
    HIDDEN_READ_IDENTITY_MAX_LENGTH,
    HIDDEN_READ_KEY_MAX_COUNT,
    HIDDEN_READ_PATH_MAX_COUNT,
    HIDDEN_READ_PATH_MAX_LENGTH,
    HIDDEN_READ_REASON_MAX_COUNT,
)
from rcp.providers.base import AgentCapability, ProviderTurnRequest
from rcp.storage.models import SpaceMachineRecord


def _scope(**changes: object) -> HiddenReadScope:
    return HiddenReadScope.model_validate(
        {
            "execution_machine": "local",
            "execution_host": "",
            "os_account": "research",
            "enforcement": HiddenReadStatus(status="enforced"),
            **changes,
        }
    )


def _machine(**changes: object) -> SpaceMachineRecord:
    return SpaceMachineRecord.model_validate(
        {
            "machine_id": "machine-1",
            "name": "Local",
            "host": "",
            "os_account": "research",
            "created_at": "",
            "updated_at": "",
            **changes,
        }
    )


def _key(path: str = "/home/research/.ssh/id_ed25519", **changes: object) -> HiddenReadKeyEvidence:
    return HiddenReadKeyEvidence.model_validate(
        {
            "path": path,
            "kind": "ssh_identity",
            "agent_confirmed": False,
            "visibility": "readable",
            **changes,
        }
    )


def test_fingerprint_is_canonical_and_covers_effective_policy() -> None:
    policy = {
        "hidden_directories": ("/secrets/b", "/secrets/a"),
        "hidden_files": ("/secrets/token-b", "/secrets/token-a"),
        "hidden_globs": ("/data/backup-*", "/data/*.sqlite3"),
        "env_allow_list": ("PATH", "HOME"),
        "key_evidence": (_key("/keys/b"), _key("/keys/a")),
        "enforcement": HiddenReadStatus(status="unhidden", reasons=("ssh_key_agent_unconfirmed",)),
    }
    first = _scope(**policy)
    reordered = _scope(
        **{
            name: value if isinstance(value, HiddenReadStatus) else tuple(reversed(value))
            for name, value in policy.items()
        }
    )
    assert first == reordered
    assert HiddenReadScope.model_validate_json(first.model_dump_json()) == first
    assert len(first.fingerprint) == 64
    variants = {
        "execution_host": "compute.example",
        "execution_machine": "remote",
        "os_account": "other",
        "hidden_directories": ("/other",),
        "hidden_files": ("/other/token",),
        "hidden_globs": ("/other/*",),
        "env_allow_list": ("HOME",),
        "enforcement": HiddenReadStatus(
            status="unhidden", reasons=("ssh_key_agent_unconfirmed", "wrapper_unavailable")
        ),
        "key_evidence": (_key("/keys/other"),),
    }
    for name, value in variants.items():
        assert _scope(**{**policy, name: value}).fingerprint != first.fingerprint
    with pytest.raises(ValidationError):
        _scope(**policy, fingerprint="0" * 64)


@pytest.mark.parametrize(
    ("kind", "reason"),
    [("ssh_identity", "ssh_key_agent_unconfirmed"), ("deploy_key", "deploy_key_agent_unconfirmed")],
)
def test_readable_key_requires_its_unhidden_reason(kind: str, reason: str) -> None:
    key = _key(kind=kind)
    with pytest.raises(ValidationError):
        _scope(key_evidence=(key,))
    with pytest.raises(ValidationError):
        _scope(
            key_evidence=(key,),
            enforcement=HiddenReadStatus(status="unhidden", reasons=("wrapper_unavailable",)),
        )
    assert _scope(
        key_evidence=(key,), enforcement=HiddenReadStatus(status="unhidden", reasons=(reason,))
    ).enforcement.reasons == (reason,)


@pytest.mark.parametrize(
    "path",
    [
        "relative",
        "~/secret",
        "",
        "/secrets/../token",
        "/secrets/./token",
        "/secrets//token",
        "//secrets/token",
        "/secrets/\x00token",
        "/secrets/\ntoken",
    ],
)
def test_path_validation_refuses_noncanonical_paths(path: str) -> None:
    for field in ("hidden_directories", "hidden_files", "hidden_globs"):
        with pytest.raises(ValidationError):
            _scope(**{field: (path,)})
    with pytest.raises(ValidationError):
        _machine(hidden_folders=[path])
    with pytest.raises(ValidationError):
        _key(path)


def test_policy_bounds() -> None:
    invalid = {
        "hidden_directories": tuple(f"/folder/{i}" for i in range(HIDDEN_READ_PATH_MAX_COUNT + 1)),
        "hidden_files": ("/" + "x" * HIDDEN_READ_PATH_MAX_LENGTH,),
        "hidden_globs": tuple(f"/glob/{i}*" for i in range(HIDDEN_READ_PATH_MAX_COUNT + 1)),
        "env_allow_list": tuple(f"ENV_{i}" for i in range(HIDDEN_READ_ENV_MAX_COUNT + 1)),
        "key_evidence": tuple(_key(f"/key/{i}") for i in range(HIDDEN_READ_KEY_MAX_COUNT + 1)),
        "execution_machine": "x" * (HIDDEN_READ_IDENTITY_MAX_LENGTH + 1),
        "execution_host": "x" * (HIDDEN_READ_IDENTITY_MAX_LENGTH + 1),
        "os_account": "x" * (HIDDEN_READ_IDENTITY_MAX_LENGTH + 1),
    }
    for name, value in invalid.items():
        with pytest.raises(ValidationError):
            _scope(**{name: value})
    with pytest.raises(ValidationError):
        _scope(env_allow_list=("X" * (HIDDEN_READ_ENV_NAME_MAX_LENGTH + 1),))
    with pytest.raises(ValidationError):
        _machine(hidden_folders=list(invalid["hidden_directories"]))
    with pytest.raises(ValidationError):
        HiddenReadStatus(
            status="unhidden", reasons=("wrapper_unavailable",) * (HIDDEN_READ_REASON_MAX_COUNT + 1)
        )


def test_duplicates_are_refused() -> None:
    for name in ("hidden_directories", "hidden_files", "hidden_globs", "env_allow_list"):
        value = "HOME" if name == "env_allow_list" else "/secret"
        with pytest.raises(ValidationError):
            _scope(**{name: (value, value)})
    with pytest.raises(ValidationError):
        _scope(hidden_files=("/secret",), hidden_directories=("/secret",))
    with pytest.raises(ValidationError):
        _scope(key_evidence=(_key(), _key()))
    with pytest.raises(ValidationError):
        _machine(hidden_folders=["/secret", "/secret"])


def test_status_requires_explicit_unique_reasons() -> None:
    for status, reasons in (
        ("unhidden", ()),
        ("enforced", ("wrapper_unavailable",)),
        ("unhidden", ("wrapper_unavailable", "wrapper_unavailable")),
    ):
        with pytest.raises(ValidationError):
            HiddenReadStatus(status=status, reasons=reasons)
    reasons = ("wrapper_unavailable", "browser_unwrapped_macos")
    assert HiddenReadStatus(status="unhidden", reasons=reasons) == HiddenReadStatus(
        status="unhidden", reasons=tuple(reversed(reasons))
    )


def test_key_evidence_requires_confirmed_public_identity_before_hiding() -> None:
    assert _key().public_key_fingerprint is None
    for changes in (
        {"visibility": "hidden"},
        {"agent_confirmed": True},
        {"public_key_fingerprint": "SHA256:invalid"},
        {"path": "/key.pub"},
    ):
        with pytest.raises(ValidationError):
            _key(**changes)
    evidence = _key(
        public_key_fingerprint="SHA256:" + "A" * 43, agent_confirmed=True, visibility="hidden"
    )
    assert evidence.visibility == "hidden"


def test_contracts_are_strict_and_immutable() -> None:
    scope = _scope(hidden_files=("/secret",))
    with pytest.raises(ValidationError):
        scope.hidden_files = ("/changed",)
    with pytest.raises(ValidationError):
        scope.enforcement.status = "unhidden"
    for changes in (
        {"hidden_files": ["/secret"]},
        {"os_account": 12},
        {"unknown": True},
        {"env_allow_list": ("TOKEN=value",)},
    ):
        with pytest.raises(ValidationError):
            _scope(**changes)
    with pytest.raises(ValidationError):
        _key(agent_confirmed="false")
    projection = MachineHiddenReadProjection(
        readiness=scope.enforcement, default_paths=("~/secret",)
    )
    with pytest.raises(ValidationError):
        projection.default_paths = ()
    assert MachineHiddenReadProjection().readiness is None
    with pytest.raises(ValidationError):
        MachineHiddenReadProjection(user_folders=("~/secret",))


@pytest.mark.parametrize("capability", get_args(AgentCapability))
def test_provider_request_defaults_are_compatible_for_every_capability(
    capability: AgentCapability,
) -> None:
    arguments = dict(
        prompt="test",
        binary="provider",
        cwd=Path("/stage"),
        model=None,
        reasoning=None,
        session_id=None,
        read_dirs=[],
        write_dirs=[],
        write_scope=None,
        capability=capability,
        provider_version=None,
    )
    assert ProviderTurnRequest(**arguments).hidden_read_scope is None
    scope = _scope()
    assert ProviderTurnRequest(**arguments, hidden_read_scope=scope).hidden_read_scope is scope


def test_machine_default_and_canonical_folder_payload() -> None:
    assert _machine().hidden_folders == []
    machine = _machine(hidden_folders=["/secret/b", "/secret/a"])
    assert machine.model_dump()["hidden_folders"] == ["/secret/a", "/secret/b"]
