"""Read access for Git in a member terminal, with no credential secrecy claim."""

from __future__ import annotations

from pathlib import Path


def terminal_git_access(
    key: Path | None,
) -> tuple[tuple[str, ...], dict[str, str]]:
    """Expose existing Git configuration and the selected checkout's deploy key.

    Read access to a deploy key conveys its write authority. The terminal runs
    as the service account and is not isolated from that account.
    """

    home = Path.home()
    paths = [home / ".gitconfig", home / ".config" / "git", home / ".ssh"]
    environment: dict[str, str] = {}
    if key is not None and key.is_file():
        paths.append(key)
    return tuple(str(path) for path in paths if path.exists()), environment
