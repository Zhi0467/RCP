"""Transcript lookup returns the shared execution-host file, never a local SSH copy."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from rcp.providers import remote_bundle
from rcp.providers.codex.remote import locate_codex_transcript


@pytest.mark.parametrize("placement", ["local", "ssh_bundle"])
@pytest.mark.parametrize("directory", ["sessions/2026/10/30", "archived_sessions"])
def test_transcript_location_uses_shared_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, placement: str, directory: str
) -> None:
    session_id = str(uuid4())
    machine = tmp_path / "machine-codex-home"
    target = machine / directory / f"rollout-2026-10-30-{session_id}.jsonl"
    target.parent.mkdir(parents=True)
    target.write_text('{"type":"session_meta"}\n')
    login = tmp_path / "provider-homes/codex" / str(uuid4())
    login.mkdir(parents=True)
    for name in ("sessions", "archived_sessions"):
        (machine / name).mkdir(exist_ok=True)
        (login / name).symlink_to(machine / name, target_is_directory=True)
    monkeypatch.setenv("CODEX_HOME", str(login))
    if placement == "local":
        found = locate_codex_transcript(session_id)
    else:
        script = remote_bundle(
            "import json, sys\nprint(json.dumps(locate_codex_transcript(sys.argv[1])))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, session_id],
            env=dict(os.environ),
            check=True,
            capture_output=True,
            text=True,
        )
        found = json.loads(result.stdout)
    assert found == str(target.resolve())
    assert target.read_text() == '{"type":"session_meta"}\n'


def test_transcript_location_requires_unique_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    session_id = str(uuid4())
    assert locate_codex_transcript(session_id) is None
    for name in ("sessions", "archived_sessions"):
        root = tmp_path / name
        root.mkdir()
        (root / f"rollout-2026-10-30-{session_id}.jsonl").write_text("{}\n")
    with pytest.raises(ValueError):
        locate_codex_transcript(session_id)


@pytest.mark.parametrize("session_id", ["../*", "*", "not-a-session", "144BFE08-6628-4BA2-B520-92487C5BC865"])
def test_transcript_location_rejects_noncanonical_identity(session_id: str) -> None:
    with pytest.raises(ValueError):
        locate_codex_transcript(session_id)
