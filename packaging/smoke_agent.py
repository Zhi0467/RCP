"""A served-app remote Discuss journey against disposable transport/provider doubles.

The SSH double runs the backend's shipped source on a separate temporary HOME;
only the network and model endpoints are replaced. No RCP module is imported by
this harness, so a source checkout cannot hide a missing frozen resource.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

HOST = "packaging-smoke.invalid"
ANSWER = "Packaged remote agent completed."


def prepare(root: Path) -> dict[str, str]:
    """Build test executables outside PATH's real provider installations."""
    home = root / "home"
    remote = root / "remote"
    binaries = root / "bin"
    remote_bin = root / "remote-bin"
    for directory in (home, remote, binaries, remote_bin):
        directory.mkdir(mode=0o700)
    (remote_bin / "python3").symlink_to(sys.executable)
    _script(
        remote_bin / "bash",
        '#!/bin/sh\nexec /bin/bash --noprofile --norc -c "$2"\n',
    )
    _script(
        remote_bin / "setsid",
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "os.setsid()\n"
        "args = sys.argv[1:]\n"
        "if args[0] == '--wait': args.pop(0)\n"
        "os.execvp(args[0], args)\n",
    )
    _script(
        binaries / "ssh",
        f"#!{sys.executable}\n"
        "import os, shlex, sys\n"
        f"host = {HOST!r}\n"
        "if host not in sys.argv: raise SystemExit('Unexpected SSH destination')\n"
        "args = sys.argv[sys.argv.index(host) + 1:]\n"
        "if len(args) != 1: raise SystemExit('Expected one remote command')\n"
        # There is no legacy account data on the simulated host. Never let its
        # maintenance request sweep the developer's real /tmp/rcp-run.* folders.
        "command = shlex.split(args[0])\n"
        "if command[:2] == ['python3', '-c'] and command[3:4] == ['sweep']:\n"
        "    raise SystemExit(0)\n"
        f"os.environ['HOME'] = {str(remote)!r}\n"
        f"os.environ['PATH'] = {str(remote_bin) + ':/usr/bin:/bin:/usr/sbin:/sbin'!r}\n"
        "os.chdir(os.environ['HOME'])\n"
        "os.execv('/bin/sh', ['sh', '-c', args[0]])\n",
    )
    _script(
        binaries / "rsync",
        f"#!{sys.executable}\n"
        "import os, shlex, sys\n"
        "args = sys.argv[1:]\n"
        f"prefix = {HOST + ':'!r}\n"
        "for i, arg in enumerate(args):\n"
        "    if arg.startswith(prefix): args[i] = shlex.split(arg[len(prefix):])[0]\n"
        "os.execv('/usr/bin/rsync', ['rsync', *args])\n",
    )
    _script(
        remote_bin / "opencode",
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "if args == ['--version']: print('1.18.33')\n"
        "elif args == ['providers', 'list']: print('[]')\n"
        "elif args == ['models', '--verbose']:\n"
        "    print('opencode/smoke\\n' + json.dumps({'name': 'Smoke', 'status': 'active'}))\n"
        "elif args == ['debug', 'skill']: print('[]')\n"
        "elif args and args[0] == 'run':\n"
        "    prompt = sys.stdin.read()\n"
        "    if not prompt: raise SystemExit('The provider received no prompt')\n"
        "    if not any((Path.cwd().parent / 'inputs').iterdir()):\n"
        "        raise SystemExit('No remote inputs were staged')\n"
        f"    Path({str(root / 'provider-ran')!r}).write_text(prompt)\n"
        f"    print(json.dumps({{'type': 'text', 'sessionID': 'ses_smoke', 'part': {{'text': {ANSWER!r}}}}}), flush=True)\n"
        "else: raise SystemExit('Unexpected provider command: ' + repr(args))\n",
    )
    return {"HOME": str(home), "PATH": f"{binaries}:/usr/bin:/bin:/usr/sbin:/sbin"}


def _script(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)


def _api(
    base_url: str, path: str, payload: dict | None = None, *, method: str | None = None
) -> dict:
    request = urllib.request.Request(
        base_url + path,
        method=method,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Origin": base_url},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"{path}: {error.code} {error.read().decode()}") from error


def verify(base_url: str, root: Path) -> None:
    _api(base_url, "/api/identity", {"display_name": "Packaging smoke"}, method="PATCH")
    repository = root / "remote" / "repository"
    repository.mkdir()
    profile = {
        "provider": "opencode",
        "model": "opencode/smoke",
        "reasoning": "",
        "location": "ssh",
        "host": HOST,
    }
    project = _api(
        base_url,
        "/api/project-setup/create",
        {
            "name": "Packaging smoke",
            "confirmed": True,
            "repositories": [
                {
                    "alias": "smoke",
                    "location": "ssh",
                    "host": HOST,
                    "path": str(repository),
                    "default_read": True,
                }
            ],
            "state_repository": "smoke",
            "execution": {"location": "ssh", "host": HOST},
            "agents": {
                name: profile
                for name in (
                    "seed",
                    "refresh",
                    "node_chat",
                    "project_chat",
                    "paper_coach",
                    "orchestrator",
                )
            },
        },
    )
    prefix = f"/api/projects/{project['id']}"
    source = repository / "preview.py"
    source.write_text("# Packaged remote preview <source>\n", encoding="utf-8")
    query = urllib.parse.urlencode({"path": str(source), "line": 1})
    with urllib.request.urlopen(
        base_url + prefix + "/repositories/files/preview?" + query, timeout=30
    ) as response:
        document = response.read().decode()
        if response.headers.get_content_type() != "text/html" or (
            "Packaged remote preview &lt;source&gt;" not in document
        ):
            raise RuntimeError(f"Packaged remote source preview failed: {document}")
    chat_id = str(uuid.uuid4())
    task = _api(
        base_url,
        prefix + "/tasks/project_chat",
        {
            "chat_id": chat_id,
            "mode": "discuss",
            "message": "Confirm the packaged agent works.",
            "run_truth_scope": ["smoke"],
        },
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        status = _api(base_url, prefix + f"/tasks/{task['operation_id']}")
        if status["status"] in {"succeeded", "failed", "cancelled"}:
            if status["status"] != "succeeded":
                raise RuntimeError(
                    f"Packaged remote agent failed: {status.get('error') or status.get('status_message')}"
                )
            transcript = _api(base_url, prefix + f"/chats/{chat_id}")
            if ANSWER not in json.dumps(transcript) or not (root / "provider-ran").is_file():
                raise RuntimeError(f"Remote provider answer missing from chat: {transcript}")
            return
        time.sleep(0.1)
    raise RuntimeError(f"Packaged remote agent timed out: {status}")
