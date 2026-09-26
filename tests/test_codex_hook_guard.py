from __future__ import annotations

import json
from pathlib import Path

import pytest

from rcp.agents.codex_hook_guard import (
    CodexHookGuardError,
    inspect_hook_sources,
    validate_control_directory,
)


def _inspect(home: Path, runtime: str = "codex.exec-json.v1", **kwargs):
    return inspect_hook_sources(
        runtime,
        codex_home=home,
        layers=kwargs.get("layers", []),
        listed_hooks=kwargs.get("listed_hooks", []),
        version="unqualified-test-version",
    )


def test_account_hooks_refused_in_both_runtimes(tmp_path):
    path = tmp_path / "hooks.json"
    path.write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "true"}]}]}})
    )
    for runtime in ("codex.exec-json.v1", "codex.app-server-stdio.v1"):
        with pytest.raises(CodexHookGuardError) as failure:
            _inspect(tmp_path, runtime)
        assert failure.value.code == "codex_foreign_hooks"
        assert failure.value.files == (str(path),)


def test_runtime_specific_inline_project_plugin_inventory(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    inline = '[hooks]\nStop=[{hooks=[{type="command",command="true"}]}]\n'
    (home / "config.toml").write_text(inline)
    project = tmp_path / "project"
    project.mkdir()
    (project / "config.toml").write_text(inline)
    plugin = tmp_path / "plugin.json"
    plugin.write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "true"}]}]}})
    )
    kwargs = {
        "layers": [{"name": {"type": "project", "dotCodexFolder": str(project)}}],
        "listed_hooks": [{"enabled": True, "sourcePath": str(plugin)}],
    }
    receipt = _inspect(home, **kwargs)
    assert receipt["warning_codes"] == ["codex_hook_sources_unqualified"]
    with pytest.raises(CodexHookGuardError) as failure:
        _inspect(home, "codex.app-server-stdio.v1", **kwargs)
    assert set(failure.value.files) == {
        str(home / "config.toml"),
        str(project / "config.toml"),
        str(plugin),
    }


def test_malformed_hook_source_fails_closed(tmp_path):
    path = tmp_path / "hooks.json"
    path.write_text("{")
    with pytest.raises(CodexHookGuardError) as failure:
        _inspect(tmp_path)
    assert failure.value.code == "codex_hook_inventory_unreadable"
    assert failure.value.files == (str(path),)


def test_control_path_checks_resolved_and_replaceable_parents(tmp_path):
    writable = tmp_path / "workspace"
    writable.mkdir()
    outside = tmp_path / "control"
    outside.mkdir()
    validate_control_directory(outside / "attempt", [writable])
    link = writable / "escape"
    link.symlink_to(outside, target_is_directory=True)
    for control in (writable / "attempt", link / "attempt", outside):
        roots = [writable] if control != outside else [outside / "nested"]
        with pytest.raises(CodexHookGuardError) as failure:
            validate_control_directory(control, roots)
        assert failure.value.code == "codex_hook_control_writable"
