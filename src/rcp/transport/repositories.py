from __future__ import annotations

from pydantic import BaseModel

from rcp.config import RepositoryConfig


class RepositoryAccess(BaseModel):
    """A pointer to a repository. RCP never copies one; it only names where it lives.

    `machine` is the space card id; `host` is that card's route as the reader sees it.
    """

    alias: str
    machine: str
    host: str = ""
    path: str


def repository_access(repository: RepositoryConfig, host: str) -> RepositoryAccess:
    """Build the pointer for a repository on a card whose route is `host`; "" is this machine."""

    return RepositoryAccess(
        alias=repository.alias,
        machine=repository.machine,
        host=host,
        path=repository.path,
    )
