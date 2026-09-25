"""Actual installed update on one pristine GitHub-hosted Ubuntu runner.

No production opt-in or test hook ships in RCP. The runner must be empty; this
program never removes an existing installation and never listens on port 8421.
"""

from __future__ import annotations

import argparse
import http.cookies
import json
import os
import pwd
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.supervisor_reboot_guest import run, service, wait_for, write_json  # noqa: E402

ROOT = Path("/opt/rcp-installed-upgrade")
DATA = Path("/home/rcp/rcp-server/data")
PROJECTS = Path("/home/rcp/rcp-server/projects")
SUPERVISOR = Path("/etc/rcp/supervisor/current")
PORT = 18421
SCRIPT = ROOT / "tests/server_installed_upgrade.py"


def preflight() -> None:
    if (
        sys.platform != "linux"
        or os.geteuid() != 0
        or os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("RCP_RUN_INSTALLED_UPGRADE") != "1"
        or not Path("/run/systemd/system").is_dir()
    ):
        raise RuntimeError("Requires explicit opt-in on a disposable GitHub Ubuntu systemd runner.")
    for path in (
        ROOT,
        Path("/etc/rcp"),
        Path("/home/rcp"),
        Path("/usr/local/bin/rcp"),
        Path("/usr/local/bin/rcp-supervisor"),
        Path("/etc/systemd/system/rcp.service"),
    ):
        if os.path.lexists(path):
            raise RuntimeError(f"Refusing an existing installation: {path}")
    try:
        pwd.getpwnam("rcp")
    except KeyError:
        pass
    else:
        raise RuntimeError("Refusing an existing service account.")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", PORT))
    run(["systemctl", "show", "--property=Version", "--value"])


def health() -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=5) as response:
        return json.load(response)


def wait_health() -> dict:
    def healthy():
        result = health()
        return result if result.get("status") == "ok" else None

    return wait_for(healthy, "Installed service did not become healthy.")


def request(path: str, *, body: dict | None = None, method: str = "GET", cookie: str = ""):
    protocol = str(health()["team_shell_protocol"]["maximum"])
    query = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Cookie": cookie,
            "RCP-Team-Shell-Protocol": protocol,
        },
    )
    return urllib.request.urlopen(query, timeout=30)


def session(fixture: dict) -> str:
    with request(
        "/api/team/session/exchange", body={"token": fixture["token"]}, method="POST"
    ) as response:
        cookies = http.cookies.SimpleCookie()
        cookies.load(response.headers["Set-Cookie"])
        assert json.load(response)["user"]["user_id"] == fixture["member_id"]
    (cookie,) = cookies.values()
    return f"{cookie.key}={cookie.value}"


def assert_serves(fixture: dict, version: str) -> None:
    running = wait_health()
    assert running["space_id"] == fixture["space_id"]
    assert running["version"] == version
    selected = json.loads(Path("/etc/rcp/supervisor/selected.json").read_text())
    assert selected["version_string"] == version
    assert running["running_commit"] == selected["commit"]
    assert os.readlink("/etc/rcp/current") == selected["release_directory"]
    with sqlite3.connect(f"file:{DATA / 'rcp.sqlite3'}?mode=ro", uri=True) as database:
        assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert database.execute(
            "SELECT state FROM provider_login_states WHERE provider='codex' AND host=''"
        ).fetchone() == ("signed_in",)
    assert (DATA / "providers/claude/qualification/setup-token").read_text() == "synthetic-login\n"
    assert json.loads((DATA / "jobs/completed/receipt.json").read_text()) == {"status": "completed"}
    assert json.loads((Path(fixture["research"]) / "cursors.json").read_text()) == {}
    empty_patches = Path(fixture["empty_patches"])
    assert empty_patches.is_dir() and not list(empty_patches.iterdir())
    cookie = session(fixture)
    with request(f"/api/projects/{fixture['project_id']}", cookie=cookie) as response:
        graph = json.load(response)["graph"]
    assert graph["nodes"]["hyp/recovery-keeps-history"]["statement"]
    with request("/api/team/space", cookie=cookie) as response:
        assert json.load(response)["space_name"] == "Installed update retained HTTP work"
    pid = int(run(["systemctl", "show", "rcp.service", "--property=MainPID", "--value"]).stdout)
    assert pid > 0 and Path(f"/proc/{pid}").stat().st_uid == pwd.getpwnam("rcp").pw_uid
    import hashlib

    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (Path(fixture["research"]) / "patches").iterdir()
    } == fixture["canonical_patches"]


def install_hook() -> None:
    (site,) = (SUPERVISOR / "lib").glob("python*/site-packages")
    shutil.copyfile(ROOT / "tests/server_installed_upgrade_hook.py", site / "sitecustomize.py")
    (site / "sitecustomize.py").chmod(0o644)


def select(bundle: Path, *, observe: bool = False, fail: bool = False) -> dict:
    write_json(ROOT / "selection.json", {"bundle": str(bundle), "observe": observe, "fail": fail})
    return json.loads(bundle.with_name(bundle.name + ".receipt.json").read_text())


def operator(*arguments: str, expected: int = 0) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["/usr/local/bin/rcp", "server", *arguments, "--machine-readable"],
        text=True,
        capture_output=True,
        timeout=1200,
    )
    # Persist event envelopes, not bootstrap codes, cookies or fixture tokens.
    log = ROOT / "operator-events.jsonl"
    with log.open("a") as stream:
        stream.write(result.stdout)
    assert result.returncode == expected, result.stdout + result.stderr
    return result


def backup() -> None:
    result = service(["/usr/local/bin/rcp", "server", "backup", "run", "--machine-readable"])
    fields = {
        field["name"]: field["value"]
        for line in result.stdout.splitlines()
        for field in json.loads(line).get("step", {}).get("fields", [])
    }
    assert fields["backup_status"] == "protected", fields
    assert fields["uncaptured_projects"] == 0, fields


def seed() -> None:
    """Runs with the old release's interpreter as its actual service account."""
    from rcp.storage import AppStore
    from tests.server_upgrade_scratch import build_scratch_wheel, populate_agent_scratch
    from tests.supervisor_reboot_data import prepare_data

    token = json.loads((ROOT / "state/enrolled.json").read_text())["token"]
    fixture = prepare_data(DATA, PROJECTS, member_token=token, empty_branch=True)
    (ROOT / "state/enrolled.json").unlink()
    wheel = build_scratch_wheel(ROOT / "state")
    populate_agent_scratch(DATA, Path(fixture["research"]), wheel)
    # Keep the cross-root hardlink within an excluded canonical file, so it is
    # realistic live-only content without making the subsequent backup partial.
    os.replace(
        Path(fixture["research"]) / "scratch-package-link",
        Path(fixture["research"]) / ".agent-run.lock",
    )
    for relative, content in {
        "providers/claude/qualification/setup-token": "synthetic-login\n",
        "jobs/completed/receipt.json": '{"status":"completed"}',
        "terminals/qualification/session.log": "retained terminal\n",
    }.items():
        path = DATA / relative
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(content)
        path.chmod(0o600)
    (Path(fixture["research"]) / "cursors.json").write_text("{}")
    empty_patches = Path(fixture["empty_patches"])
    assert empty_patches.is_dir() and not list(empty_patches.iterdir())
    store = AppStore(DATA / "rcp.sqlite3")
    store.mark_provider_login_verified(
        "codex", "", member_id=fixture["member_id"], detail="Synthetic offline login fixture"
    )
    store.mark_provider_login_signed_out(
        "claude",
        "",
        member_id=fixture["member_id"],
        source="sign_out",
        detail="A replacement login is awaiting verification.",
    )
    for name in ("facts", "paper"):
        (Path(fixture["research"]) / name).mkdir(exist_ok=True, mode=0o700)
    write_json(ROOT / "state/fixture.json", fixture)


def setup() -> None:
    # This helper executes under the historical wheel, not the checkout's RCP.
    from tests import supervisor_reboot_guest as guest

    guest.ROOT = ROOT
    guest.STATE = ROOT / "state"
    guest.setup()
    dropin = Path("/etc/systemd/system/rcp.service.d")
    dropin.mkdir()
    (dropin / "installed-upgrade.conf").write_text(
        "[Service]\nExecStart=\n"
        f"ExecStart=/usr/local/bin/rcp serve --host 127.0.0.1 --port {PORT} --web-assets prebuilt\n"
    )
    run(["systemctl", "daemon-reload"])


def drive(base: Path, candidate: Path, output: Path, tag: str, uv: Path) -> None:
    preflight()
    ROOT.mkdir(mode=0o755)
    (ROOT / "tests").mkdir(mode=0o755)
    workspace = Path(__file__).resolve().parents[1]
    for name in (
        "__init__.py",
        "server_installed_upgrade.py",
        "server_installed_upgrade_hook.py",
        "server_upgrade_scratch.py",
        "supervisor_reboot_guest.py",
        "supervisor_reboot_data.py",
    ):
        shutil.copyfile(workspace / "tests" / name, ROOT / "tests" / name)
    (ROOT / "bundles").mkdir(mode=0o755)
    for name, source in (("base", base), ("target", candidate)):
        shutil.copytree(source, ROOT / "bundles" / name)
        shutil.copyfile(
            source.with_name(source.name + ".receipt.json"),
            ROOT / "bundles" / f"{name}.receipt.json",
        )
    base, candidate = ROOT / "bundles/base", ROOT / "bundles/target"
    # setup-uv's binary belongs to the runner; privileged installation requires a
    # root-owned executable and ancestors, just as on the documented server.
    if uv.resolve() != Path("/usr/local/bin/uv"):
        run(["install", "-m", "0755", str(uv), "/usr/local/bin/uv"])
    os.environ["PATH"] = "/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    python = ROOT / "bootstrap/bin/python"
    run(["uv", "venv", "--managed-python", "--python", "3.12", str(python.parent.parent)])
    run(
        [
            "uv",
            "pip",
            "sync",
            "--python",
            str(python),
            "--require-hashes",
            "--only-binary",
            ":all:",
            str(base / "requirements.lock.txt"),
        ],
        timeout=600,
    )
    run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--no-deps",
            *map(str, base.glob("*.whl")),
        ]
    )
    run([str(python), "-I", str(SCRIPT), "setup"], timeout=1200)
    select(base)
    install_hook()
    first = operator("install", "--team-name", "Installed upgrade", expected=3)
    events = [json.loads(line) for line in first.stdout.splitlines()]
    (action,) = events[-1]["step"]["actions"]
    initialized = run(
        ["script", "--quiet", "--return", "--command", shlex.join(action["argv"]), "/dev/null"]
    ).stdout
    (code,) = re.findall(r"rcp_bootstrap_[A-Za-z0-9_-]{16}\.[A-Za-z0-9_-]{43}", initialized)
    operator("install", "--team-name", "Installed upgrade")
    wait_health()
    with request(
        "/api/team/enroll", body={"code": code, "display_name": "Upgrade researcher"}, method="POST"
    ) as response:
        token = json.load(response)["token"]
    write_json(ROOT / "state/enrolled.json", {"token": token}, service_owned=True)
    selected = json.loads(Path("/etc/rcp/supervisor/selected.json").read_text())
    old_python = str(Path(selected["release_directory"]) / ".venv/bin/python")
    service([old_python, "-I", str(SCRIPT), "seed"])
    wait_health()
    fixture = json.loads((ROOT / "state/fixture.json").read_text())
    cookie = session(fixture)
    with request(
        "/api/team/space",
        method="PATCH",
        body={"name": "Installed update retained HTTP work"},
        cookie=cookie,
    ) as response:
        assert response.status == 200
    assert_serves(fixture, selected["version_string"])
    destination = Path("/var/backups/rcp-installed-upgrade")
    destination.mkdir(mode=0o700)
    account = pwd.getpwnam("rcp")
    os.chown(destination, account.pw_uid, account.pw_gid)
    operator(
        "backup",
        "configure",
        "--destination",
        str(destination),
        "--schedule",
        "02:00",
        "--retention",
        "30",
        "--confirm",
    )
    backup()
    service(
        [
            old_python,
            "-I",
            "-c",
            (
                "import os, pathlib, py_compile, sysconfig; os.umask(0o002); "
                "stdlib = pathlib.Path(sysconfig.get_path('stdlib')); "
                "cache = stdlib / '__pycache__'; cache.mkdir(exist_ok=True); "
                "py_compile.compile(str(stdlib / '_pydecimal.py'), cfile=str(cache / 'upgrade-dirty.pyc'), doraise=True); "
                "cache.chmod(0o775); (cache / 'upgrade-dirty.pyc').chmod(0o664)"
            ),
        ]
    )
    target = select(candidate)
    operator("supervisor", "update")
    install_hook()  # The new supervisor is independently installed, not patched in its wheel.
    assert_serves(fixture, selected["version_string"])
    confirmation = f"{target['tag']}:{target['manifest_sha256']}"
    select(candidate, observe=True, fail=True)
    failed = operator("update", "--confirm-target", confirmation, expected=1)
    assert "installed-upgrade intentional candidate failure" in failed.stdout
    exact = json.loads((ROOT / "rollback-exact.json").read_text())
    journal = json.loads(
        (Path("/etc/rcp/supervisor/operations") / f"{exact['operation_id']}.json").read_text()
    )
    assert journal["phase"] == "rolled_back"
    assert exact["roots"] >= 2
    assert_serves(fixture, selected["version_string"])
    service([old_python, "-I", str(SCRIPT), "scratch-check"])
    backup()
    select(candidate, observe=True)
    operator("update", "--confirm-target", confirmation)
    records = [
        json.loads(path.read_text())
        for path in Path("/etc/rcp/supervisor/operations").glob("*.json")
    ]
    assert any(
        record["phase"] == "committed"
        and record["target"]["manifest_sha256"] == target["manifest_sha256"]
        for record in records
    )
    # The committed update removes every snapshot and the failed attempt's quarantine.
    leftovers = [
        *Path("/home/rcp/rcp-server/update-checkpoints").iterdir(),
        *(path for path in Path("/home/rcp").rglob(".rcp-checkpoint-*")),
    ]
    assert leftovers == [], leftovers
    from zipfile import ZipFile

    (wheel,) = candidate.glob("rcp-*.whl")
    with ZipFile(wheel) as archive:
        version = next(
            line.removeprefix("Version: ")
            for line in archive.read(
                next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
            )
            .decode()
            .splitlines()
            if line.startswith("Version: ")
        )
    assert_serves(fixture, version)
    current = json.loads(Path("/etc/rcp/supervisor/selected.json").read_text())
    service(
        [
            str(Path(current["release_directory"]) / ".venv/bin/python"),
            "-I",
            str(SCRIPT),
            "scratch-check",
        ]
    )
    backup()
    write_json(
        output / "result.json",
        {
            "status": "passed",
            "release": tag,
            "rollback_exact": exact,
            "candidate_version": version,
            "backup_after_rollback": "protected",
            "port": PORT,
        },
    )


def main() -> None:
    # Isolated installed interpreters need only the test helpers, never src/rcp.
    if len(sys.argv) == 2 and sys.argv[1] in {"setup", "seed", "scratch-check"}:
        if (
            sys.platform != "linux"
            or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("RCP_RUN_INSTALLED_UPGRADE") != "1"
            or ROOT.stat().st_uid != 0
        ):
            raise RuntimeError("Installed helper requires the disposable runner fixture.")
        if sys.argv[1] == "setup":
            setup()
        elif sys.argv[1] == "seed":
            seed()
        else:
            from tests.server_upgrade_scratch import assert_scratch_usable

            assert_scratch_usable(DATA / "run-stage/real-tools")
        return
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("base", "candidate", "output", "uv"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    arguments = parser.parse_args()
    arguments.output.mkdir(parents=True, exist_ok=True)
    preflight()  # Refusal must never reach cleanup of a pre-existing installation.
    try:
        drive(arguments.base, arguments.candidate, arguments.output, arguments.tag, arguments.uv)
    finally:
        # Diagnostics remain on the disposable runner; tokens and raw fixture
        # files are deliberately excluded from uploaded artifacts.
        if ROOT.exists():
            logs = run(["journalctl", "-u", "rcp.service", "--no-pager"])
            (arguments.output / "systemd.log").write_text(logs.stdout)
            if (ROOT / "operator-events.jsonl").exists():
                shutil.copyfile(
                    ROOT / "operator-events.jsonl", arguments.output / "operator-events.jsonl"
                )
            if Path("/etc/systemd/system/rcp.service").exists():
                run(["systemctl", "stop", "rcp.service"])


if __name__ == "__main__":
    main()
