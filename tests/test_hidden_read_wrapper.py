from __future__ import annotations

import ast
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from rcp.agents.hidden_read import HIDDEN_READ_ENV_DENY_LIST, staged_hidden_read_source
from rcp.agents.staged_hidden_read import (
    bwrap_argv,
    clean_environment,
    glob_path_regex,
    main,
    probe_hidden_read_wrapper,
    render_sandbox_profile,
)


def test_source_is_standalone_python39() -> None:
    tree = ast.parse(staged_hidden_read_source(), feature_version=(3, 9))
    assert not any(
        isinstance(node, ast.ImportFrom) and (node.module or "").startswith("rcp")
        for node in ast.walk(tree)
    )


def test_seatbelt_profile_is_allow_default_with_escaped_path_denies() -> None:
    policy = {
        "hidden_directories": ['/private/a"b'],
        "hidden_files": ["/token\\file"],
        "hidden_globs": ["/data/backup-*"],
    }
    rendered = render_sandbox_profile(policy)
    assert rendered.splitlines()[:2] == ["(version 1)", "(allow default)"]
    for selector, path in (("subpath", '/private/a"b'), ("literal", "/token\\file")):
        assert f"(deny file-read* file-write* ({selector} {json.dumps(path)}))" in rendered
    assert "(regex #" in rendered
    pattern = re.compile(glob_path_regex("/data/backup-*"))
    assert pattern.fullmatch("/data/backup-123/token")
    assert not pattern.fullmatch("/data/not-a-backup-123")
    for glob_pattern, matches, misses in (
        (
            "/copy/**/rcp.sqlite3*",
            ("/copy/rcp.sqlite3", "/copy/a/b/rcp.sqlite3-wal"),
            ("/copy/a/other",),
        ),
        ("/data[[]x]/*", ("/data[x]/secret",), ("/datax/secret",)),
        ("/file[ab]?", ("/fileax",), ("/filecx",)),
    ):
        matcher = re.compile(glob_path_regex(glob_pattern))
        assert all(matcher.fullmatch(path) for path in matches)
        assert not any(matcher.fullmatch(path) for path in misses)
    assert "network" not in rendered
    assert "mach-lookup" not in rendered


def test_bwrap_hides_future_glob_matches_and_keeps_other_entries(tmp_path: Path) -> None:
    data = tmp_path / "data"
    (data / "run-stage" / "task").mkdir(parents=True)
    (data / "rcp.sqlite3").touch()
    (data / "tools").mkdir()
    (data / "providers").mkdir()
    (data / "link").symlink_to("tools")
    secret = data / "run-stage" / "task" / "secret"
    secret.mkdir()
    token = tmp_path / "token"
    token.touch()
    policy = {
        "hidden_directories": [str(data / "providers"), str(secret), str(tmp_path / "absent")],
        "hidden_files": [str(token), str(tmp_path / "absent-file"), str(data / "tools" / "later")],
        "hidden_globs": [str(data) + "/rcp.sqlite3*"],
        # The home is never emptied for a missing literal directly inside it.
        "account_home": str(tmp_path),
    }
    argv = bwrap_argv(policy, "exit 23")
    data = Path(os.path.realpath(data))
    assert argv[:4] == ["bwrap", "--dev-bind", "/", "/"]
    assert argv[-4:] == ["--", "/bin/bash", "-c", "exit 23"]
    mounts = argv[4:-4]
    # The parent is emptied first, so a WAL or copy created later never appears.
    assert mounts[:2] == ["--tmpfs", str(data)]
    assert ["--symlink", "tools", str(data / "link")] == mounts[2:5]
    assert ["--bind", str(data / "run-stage"), str(data / "run-stage")] == mounts[5:8]
    assert ["--bind", str(data / "tools"), str(data / "tools")] == mounts[8:11]
    # A literal that does not exist yet is hidden by emptying its existing parent.
    assert ["--tmpfs", str(data / "tools")] == mounts[11:13]
    # Masks inside a bound entry come after it; hidden or matching entries stay unbound.
    assert mounts[13:] == [
        "--tmpfs",
        os.path.realpath(secret),
        "--ro-bind",
        "/dev/null",
        os.path.realpath(token),
    ]


@pytest.mark.parametrize(
    "available,returncode,reason",
    [(False, 0, "wrapper_unavailable"), (True, 1, "userns_blocked"), (True, 0, None)],
)
def test_probe_reports_tool_and_namespace_readiness(monkeypatch, available, returncode, reason):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/bwrap" if available else None)
    monkeypatch.setattr(os, "access", lambda *args: available)
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, returncode)
    )
    result = probe_hidden_read_wrapper()
    assert result["ready"] == (available and not returncode)
    assert result["reason"] == reason


def test_fallback_executes_with_clean_env_and_visible_reason(tmp_path, monkeypatch, capsys):
    from rcp.agents import staged_hidden_read

    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"env_deny_list": ["*SECRET*"]}))
    monkeypatch.setattr(os, "environ", {"HOME": "/home/research", "lab_secret": "value"})
    monkeypatch.setattr(
        staged_hidden_read,
        "probe_hidden_read_wrapper",
        lambda: {"ready": False, "reason": "userns_blocked"},
    )
    calls = []

    class Executed(Exception):
        pass

    def execute(*args):
        calls.append(args)
        raise Executed

    monkeypatch.setattr(os, "execve", execute)
    with pytest.raises(Executed):
        main(["--policy", str(policy), "-lc", "exit 29"])
    assert calls == [("/bin/bash", ["/bin/bash", "-c", "exit 29"], {"HOME": "/home/research"})]
    assert capsys.readouterr().err


@pytest.mark.parametrize("shell_option", ["-c", "-lc"])
def test_real_wrapper_hides_content_preserves_environment_streams_cwd_and_exit(
    tmp_path: Path, shell_option: str
) -> None:
    readiness = probe_hidden_read_wrapper()
    if not readiness["ready"]:
        pytest.skip(readiness["reason"])
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(staged_hidden_read_source())
    secret = tmp_path / "secret"
    public = tmp_path / "public"
    secret.write_text("PRIVATE-CONTENT")
    public.write_text("public-data")
    policy = {"hidden_files": [str(secret)], "env_deny_list": HIDDEN_READ_ENV_DENY_LIST}
    (tmp_path / "policy.json").write_text(json.dumps(policy))
    script = "\n".join(
        [
            'test -z "$CLAUDE_CODE_OAUTH_TOKEN" || exit 1',
            f'test "$(cat {shlex.quote(str(secret))} 2>/dev/null)" != PRIVATE-CONTENT || exit 2',
            f'test "$(cat {shlex.quote(str(public))})" = public-data || exit 3',
            f'test "$PWD" = {shlex.quote(str(tmp_path))} || exit 4',
            'read -r input; printf "%s" "$input"',
            "printf stderr-marker >&2",
            "exit 29",
        ]
    )
    env = {
        **os.environ,
        "CLAUDE_CODE_OAUTH_TOKEN": "private-token",
        "RCP_HIDDEN_READ_POLICY": str(tmp_path / "policy.json"),
    }
    result = subprocess.run(
        [sys.executable, str(wrapper), shell_option, script],
        cwd=tmp_path,
        env=env,
        input="stdin-marker\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 29, result.stderr
    assert result.stdout == "stdin-marker"
    assert result.stderr == "stderr-marker"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in clean_environment(policy, env)
