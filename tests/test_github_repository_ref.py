from __future__ import annotations

import socket
from pathlib import Path

import pytest
from pydantic import ValidationError

from rcp.server_ops.github import GitHubRepositoryRef, parse_github_repository_ref


@pytest.mark.parametrize(
    ("value", "identity"),
    [
        ("https://github.com/OpenAI/RCP", "openai/rcp"),
        ("https://github.com/OpenAI/RCP.git", "openai/rcp"),
        ("git@github.com:OpenAI/RCP", "openai/rcp"),
        ("git@github.com:OpenAI/RCP.git", "openai/rcp"),
        ("https://github.com/a/r", "a/r"),
        ("https://github.com/a-b/repo.name_with-parts", "a-b/repo.name_with-parts"),
        ("https://github.com/openai/rcp.git.git", "openai/rcp.git"),
    ],
)
def test_repository_ref_normalizes_only_the_two_accepted_forms(
    value: str,
    identity: str,
) -> None:
    reference = parse_github_repository_ref(value)

    assert reference == GitHubRepositoryRef(identity=identity)
    assert reference.https_clone_url == f"https://github.com/{identity}.git"
    assert reference.ssh_clone_url == f"git@github.com:{identity}.git"
    assert reference.settings_url == f"https://github.com/{identity}/settings/keys"
    assert reference.model_dump(mode="json") == {"identity": identity}


@pytest.mark.parametrize(
    "value",
    [
        # Not one of the two accepted URL shapes.
        "https://token@github.com/openai/rcp.git",
        "https://github.com:443/openai/rcp",
        "http://github.com/openai/rcp",
        "ssh://git@github.com/openai/rcp.git",
        "git@github.example:openai/rcp",
        "file:///srv/rcp",
        "/srv/rcp",
        # Right prefix, wrong number of path segments.
        "https://github.com/openai/../rcp",
        "https://github.com/openai/rcp/extra",
        "git@github.com:openai",
        # Untrimmed or carrying a control character. Nothing else may report these.
        " ../openai/rcp ",
        "https://github.com/openai/rcp\n",
        "https://github.com/openai/rcp\x7f",
        "https://github.com/openai/rcp\x00",
        # A space is not a control character, so it must fall through to the
        # component patterns rather than be reported as untrimmed.
        "https://github.com/open ai/rcp",
        "https://github.com/openai/rc p",
        "git@github.com:open_ai/rcp",
        "git@github.com:-openai/rcp",
        "git@github.com:openai-/rcp",
        f"git@github.com:{'a' * 40}/rcp",
        "https://github.com/openai/rcp.git?token=value",
        "https://github.com/openai/rcp#fragment",
        "https://github.com/openai/rcp%2Fother",
        "git@github.com:openai/.",
        "git@github.com:openai/..",
        "git@github.com:openai/.git",
        f"git@github.com:openai/{'r' * 101}",
    ],
)
def test_repository_ref_rejects_ambiguous_or_non_github_sources_before_io(
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject invalid references before network or filesystem access."""

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: pytest.fail("repository parsing performed DNS"),
    )
    monkeypatch.setattr(
        Path,
        "stat",
        lambda *_args, **_kwargs: pytest.fail("repository parsing inspected the filesystem"),
    )

    with pytest.raises(ValueError):
        parse_github_repository_ref(value)


def test_repository_ref_rejects_a_non_string_before_touching_it() -> None:
    with pytest.raises(ValueError):
        parse_github_repository_ref(b"https://github.com/openai/rcp")  # type: ignore[arg-type]


def test_owner_and_repository_split_the_identity_in_that_order() -> None:
    reference = parse_github_repository_ref("https://github.com/OpenAI/RCP.git")

    assert reference.owner == "openai"
    assert reference.repository == "rcp"


def test_persisted_reference_cannot_be_widened_or_rewritten_after_validation() -> None:
    reference = GitHubRepositoryRef(identity="openai/rcp")

    # frozen: the validated identity is the identity for the object's whole life.
    with pytest.raises(ValidationError):
        reference.identity = "attacker/repo"  # type: ignore[misc]

    # strict: no quiet coercion into the validated field.
    with pytest.raises(ValidationError):
        GitHubRepositoryRef(identity=b"openai/rcp")  # type: ignore[arg-type]


def test_persistable_reference_requires_the_canonical_lowercase_identity() -> None:
    with pytest.raises(ValidationError):
        GitHubRepositoryRef(identity="OpenAI/RCP")

    with pytest.raises(ValidationError, match="owner"):
        GitHubRepositoryRef(identity="open_ai/rcp")

    with pytest.raises(ValidationError, match="name"):
        GitHubRepositoryRef(identity="openai/..")
