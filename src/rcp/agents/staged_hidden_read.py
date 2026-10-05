#!/usr/bin/env python3
"""Standalone tool-shell wrapper; this exact source is shipped to execution hosts.

Policy JSON is a serialized HiddenReadScope. Shell use takes its policy from
RCP_HIDDEN_READ_POLICY (or <wrapper>.policy.json). No RCP imports: Python 3.9+.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TypedDict


class WrapperReadiness(TypedDict):
    ready: bool
    platform: str
    reason: str | None
    tool_present: bool
    userns_works: bool | None


# Standalone probe bound; limits.py is owned by the frozen contract slice.
PROBE_TIMEOUT_SECONDS = 5


def probe_hidden_read_wrapper(*, timeout=PROBE_TIMEOUT_SECONDS) -> WrapperReadiness:
    platform = sys.platform
    tool = "/usr/bin/sandbox-exec" if platform == "darwin" else shutil.which("bwrap")
    present = bool(tool and os.access(tool, os.X_OK))
    result: WrapperReadiness = {
        "ready": False,
        "platform": platform,
        "reason": "wrapper_unavailable",
        "tool_present": present,
        "userns_works": None,
    }
    if platform not in ("darwin", "linux") or not present:
        return result
    command = (
        [tool, "-p", "(version 1)(allow default)", "/usr/bin/true"]
        if platform == "darwin"
        else [tool, "--dev-bind", "/", "/", "--", "/bin/true"]
    )
    try:
        checked = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
        ready = checked.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ready = False
    result.update(
        ready=ready,
        userns_works=ready if platform == "linux" else None,
        reason=None
        if ready
        else ("userns_blocked" if platform == "linux" else "wrapper_unavailable"),
    )
    return result


def host_facts(paths=()):
    """Resolve on the actual account, including absent paths and login overrides."""
    home = os.path.realpath(os.path.expanduser("~"))
    return {
        "home": home,
        "os_account": pwd.getpwuid(os.geteuid()).pw_name,
        "paths": {path: os.path.realpath(os.path.expanduser(path)) for path in paths},
        "provider_login_files": [
            os.path.realpath(
                os.path.join(os.environ.get("CODEX_HOME", home + "/.codex"), "auth.json")
            ),
            os.path.realpath(
                os.path.join(
                    os.environ.get("CLAUDE_CONFIG_DIR", home + "/.claude"), ".credentials.json"
                )
            ),
            os.path.realpath(
                os.path.join(
                    os.environ.get("XDG_DATA_HOME", home + "/.local/share"), "opencode/auth.json"
                )
            ),
        ],
        "readiness": probe_hidden_read_wrapper(),
    }


def glob_path_regex(pattern):
    """Path glob to Seatbelt regex, including recursive copies and literal escapes."""
    pieces = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if pattern[index : index + 3] == "**/":
            pieces.append("(.*/)?")
            index += 3
            continue
        if char == "*":
            pieces.append("[^/]*")
        elif char == "?":
            pieces.append("[^/]")
        elif char == "[":
            end = pattern.find("]", index + 1)
            if end < 0:
                pieces.append(r"\[")
            else:
                content = pattern[index + 1 : end]
                if content in ("[", "*", "?"):
                    pieces.append(re.escape(content))
                else:
                    if content.startswith("!"):
                        content = "^" + content[1:]
                    elif content.startswith("^"):
                        content = "\\" + content
                    pieces.append("[" + content.replace("\\", "\\\\") + "]")
                index = end
        else:
            pieces.append(re.escape(char))
        index += 1
    # A directory match hides descendants too, just like its tmpfs mask.
    return "^" + "".join(pieces) + "(/.*)?$"


def render_sandbox_profile(policy):
    lines = ["(version 1)", "(allow default)"]
    for field, selector in (
        ("hidden_directories", "subpath"),
        ("hidden_files", "literal"),
        ("hidden_globs", "regex"),
    ):
        for path in policy.get(field, ()):
            value = glob_path_regex(path) if selector == "regex" else path
            literal = json.dumps(value, ensure_ascii=False)
            lines.append(
                "(deny file-read* file-write* ("
                + selector
                + " "
                + ("#" if selector == "regex" else "")
                + literal
                + "))"
            )
    return "\n".join(lines) + "\n"


def bwrap_argv(policy, command, *, executable="bwrap"):
    directories = set(policy.get("hidden_directories", ()))
    files = set(policy.get("hidden_files", ()))
    for pattern in policy.get("hidden_globs", ()):
        for path in glob.glob(pattern, recursive=True):
            (directories if os.path.isdir(path) else files).add(path)
    # Mount targets are canonical on this host; avoid mounting into a parent
    # already masked, where bwrap could no longer find the nested target.
    directories = sorted({os.path.realpath(path) for path in directories if os.path.isdir(path)})
    roots = []
    for path in directories:
        if not any(path == root or path.startswith(root + "/") for root in roots):
            roots.append(path)
    argv = [executable, "--dev-bind", "/", "/"]
    for path in roots:
        argv.extend(["--tmpfs", path])
    for path in sorted({os.path.realpath(path) for path in files if os.path.isfile(path)}):
        if not any(path == root or path.startswith(root + "/") for root in roots):
            argv.extend(["--ro-bind", "/dev/null", path])
    return [*argv, "--", "/bin/bash", "-c", command]


def install(directory, files):
    """Atomically place read-only launch files in a private RCP-owned directory."""
    target = Path(directory)
    # `parents=True` would give new parents, such as `~/.rcp`, the umask's mode.
    for path in (*reversed(target.parents), target):
        if not path.exists():
            path.mkdir(mode=0o700, exist_ok=True)
    # `~/.rcp` must not be writable by others; its two private levels are 0700.
    for path, open_bits in ((target.parent.parent, 0o022), (target.parent, 0o077), (target, 0o077)):
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & open_bits
        ):
            raise ValueError("hidden-read wrapper directory must be a private owned folder")
    for name, content in files.items():
        descriptor, temporary = tempfile.mkstemp(prefix="." + name + ".", dir=str(target))
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.chmod(temporary, 0o400)
        os.replace(temporary, target / name)


def clean_environment(policy, environ):
    return {name: environ[name] for name in policy["env_allow_list"] if name in environ}


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--host-facts")
    parser.add_argument("--install")
    # argparse interprets -lc as clustered short flags unless normalized.
    arguments = ["-c" if value == "-lc" else value for value in arguments]
    parser.add_argument("-c", dest="command")
    args = parser.parse_args(arguments)
    if args.probe:
        print(json.dumps(probe_hidden_read_wrapper()))
        return 0
    if args.host_facts is not None:
        print(json.dumps(host_facts(json.loads(args.host_facts))))
        return 0
    if args.install is not None:
        install(args.install, json.load(sys.stdin))
        return 0
    if args.command is None:
        parser.error("-c requires a command")
    policy_path = (
        args.policy or os.environ.get("RCP_HIDDEN_READ_POLICY") or __file__ + ".policy.json"
    )
    with open(policy_path, encoding="utf-8") as stream:
        policy = json.load(stream)
    env = clean_environment(policy, os.environ)
    readiness = probe_hidden_read_wrapper()
    if not readiness["ready"]:
        print("RCP hidden-read fallback: " + readiness["reason"], file=sys.stderr)
        os.execve("/bin/bash", ["/bin/bash", "-c", args.command], env)
    if sys.platform == "darwin":
        # An inline profile leaves no per-command file; exec preserves streams,
        # cwd, signals and exit status.
        os.execve(
            "/usr/bin/sandbox-exec",
            [
                "/usr/bin/sandbox-exec",
                "-p",
                render_sandbox_profile(policy),
                "/bin/bash",
                "-c",
                args.command,
            ],
            env,
        )
    else:
        executable = shutil.which("bwrap")
        os.execve(executable, bwrap_argv(policy, args.command, executable=executable), env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
