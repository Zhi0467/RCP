from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from rcp_supervisor import root_tools
from rcp_supervisor.errors import SupervisorError


def test_root_tool_ignores_process_path_and_returns_verified_resolved_path(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/service/writable/bin")
    seen = []

    def locate(name, *, path):
        seen.append((name, path))
        return "/bin/sh"

    monkeypatch.setattr(root_tools.shutil, "which", locate)
    assert root_tools.root_executable("sh") == str(Path("/bin/sh").resolve())
    assert seen == [("sh", "/usr/bin:/bin:/usr/local/bin")]


@pytest.mark.parametrize("unsafe", ["owner", "ancestor", "writable", "special"])
def test_root_tool_refuses_unsafe_executable_or_ancestor(monkeypatch, unsafe) -> None:
    executable = Path("/usr/bin/tool")
    monkeypatch.setattr(root_tools.shutil, "which", lambda name, *, path: str(executable))
    monkeypatch.setattr(Path, "resolve", lambda self, *, strict: self)

    def metadata(path):
        regular = path == executable
        mode = (stat.S_IFREG | 0o755) if regular else (stat.S_IFDIR | 0o755)
        uid = 0
        if unsafe == "owner" and regular or unsafe == "ancestor" and path == executable.parent:
            uid = 1000
        if unsafe == "writable" and path == executable.parent:
            mode |= 0o020
        if unsafe == "special" and regular:
            mode = stat.S_IFIFO | 0o700
        return SimpleNamespace(st_mode=mode, st_uid=uid)

    monkeypatch.setattr(Path, "lstat", metadata)
    with pytest.raises(SupervisorError, match="unsafe owner or path"):
        root_tools.root_executable("tool")


@pytest.mark.parametrize("name", ["", "..", "/tmp/tool", "../tool", "bad\x00tool"])
def test_root_tool_name_cannot_bypass_fixed_path(name) -> None:
    with pytest.raises(SupervisorError, match="fixed executable path"):
        root_tools.root_executable(name)


def test_missing_root_tool_is_an_explicit_failure(monkeypatch) -> None:
    monkeypatch.setattr(root_tools.shutil, "which", lambda name, *, path: None)
    with pytest.raises(SupervisorError, match="unavailable"):
        root_tools.root_executable("missing")
